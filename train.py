import argparse
import os.path

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.utils.data
import torch.utils.data.distributed
import torch.utils.tensorboard
import torchvision.transforms as transforms

import frostml.core.dist as dist
import frostml.module
import frostml.utils as utils
import frostml.vision.models as models
import frostml.vision.datasets as datasets


def init_parser():
    parser = argparse.ArgumentParser('', add_help=False)

    # required argument
    parser.add_argument('data', default=None, type=str, help='the path to dataset')

    # arguments
    parser.add_argument('--device', default='cuda', type=str, help='the device to use, disabled at distributed mode')
    parser.add_argument('--epochs', default=300, type=int, help='the number of total iterations to run (default: 300)')
    parser.add_argument('--resume', default=None, type=str, help='resume training from the checkpoint')
    parser.add_argument('--start-epoch', default=0, type=int, help='the manual epoch number (useful on restarts)')
    parser.add_argument('--seed', default=None, type=int, help='the seed for reproducibility')
    parser.add_argument('--save-dir', default=None, type=str, help='the checkpoint will be saved here')
    parser.add_argument('--tensorboard-dir', default=None, type=str, help='Tensorboard log will be saved here')

    # arguments for model
    parser.add_argument('--num-classes', default=1000, type=int, help='the number of classes in the dataset')

    # arguments for dataloader
    parser.add_argument('--batch-size', default=64, type=int, help='mini batch size for each device (default: 64)')
    parser.add_argument('--workers', default=8, type=int, help='the number of dataloader workers (default: 8)')
    parser.add_argument('--pin-memory', action='store_true', help='pin memory for more efficient data transfer')
    parser.add_argument('--no-pin-memory', action='store_false', dest='pin-memory')
    parser.set_defaults(pin_memory=True)

    # arguments for optimizer
    parser.add_argument('--lr', default=1e-3, type=float, help='initial learning rate')

    # arguments for learning scheduler
    parser.add_argument('--step-size', default=30, type=int, help='')
    parser.add_argument('--gamma', default=1e-1, type=float, help='')

    # arguments for distributed data parallel mode
    parser.add_argument('--world-size', default=1, type=int, help='the number of nodes for distributed mode')
    parser.add_argument('--dist-url', default='env://', type=str, help='the url for distributed mode')
    return parser


def main(args):
    dist.initialize_distributed_mode(args)

    # set reproducibility option
    utils.enable_reproducibility(args.seed, args.distributed)

    # set device
    device = torch.device(args.device)

    # initialize model
    model = models.efficientformer_l1()
    model.to(device)

    model_non_distributed = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        model_non_distributed = model.module

    # initialize dataloader
    trainloader, validloader, trainsampler, _= build_dataloader(args)

    # initialize objects for optimization
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.gamma)
    criterion = nn.CrossEntropyLoss()
    evaluator = frostml.module.Accuracy(topk=(1, 5))

    # resume from the checkpoint
    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        model_non_distributed.load_state_dict(checkpoint['model'])
        if 'optimizer' in checkpoint and 'scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            scheduler.load_state_dict(checkpoint['scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1
        scheduler.step(args.start_epoch)

    # initialize vars, writers
    if args.save_dir:
        if not os.path.exists(args.save_dir):
            os.makedirs(args.save_dir)

    if args.tensorboard_dir:
        if dist.is_main_process():
            args.writer = torch.utils.tensorboard.SummaryWriter(log_dir=args.tensorboard_dir)
        else:
            args.writer = None

    best_accuracy = 0.

    # start train and valid iterations
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            trainsampler.set_epoch(epoch)

        train(trainloader, model, optimizer, criterion, evaluator, epoch, args)
        score = valid(validloader, model, criterion, evaluator, epoch, args)
        scheduler.step()

        checkpoint = {
            'epoch': epoch,
            'model': model_non_distributed.state_dict(),
            'optimizer': optimizer.state_dict(),
            'scheduler': scheduler.state_dict(),
        }

        if args.save_dir:
            dist.save_on_main_process(checkpoint, os.path.join(args.save_dir, 'checkpoint.pth'))

        if best_accuracy < score:
            best_accuracy = score
            if args.save_dir:
                dist.save_on_main_process(checkpoint, os.path.join(args.save_dir, 'best_checkpoint.pth'))


def build_dataloader(args):
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(224),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    valid_transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])
    trainset = datasets.ImageNet(args.data, train=True, transform=train_transform)
    validset = datasets.ImageNet(args.data, train=False, transform=valid_transform)
    if args.distributed:
        trainsampler = torch.utils.data.distributed.DistributedSampler(trainset, shuffle=True)
        validsampler = torch.utils.data.distributed.DistributedSampler(validset, shuffle=False)
    else:
        trainsampler = torch.utils.data.RandomSampler(trainset)
        validsampler = torch.utils.data.SequentialSampler(validset)
    trainloader = torch.utils.data.DataLoader(
        trainset, batch_size=args.batch_size, num_workers=args.workers,
        pin_memory=args.pin_memory, sampler=trainsampler, drop_last=True,
    )
    validloader = torch.utils.data.DataLoader(
        validset, batch_size=args.batch_size, num_workers=args.workers,
        pin_memory=args.pin_memory, sampler=validsampler, drop_last=True,
    )
    return trainloader, validloader, trainsampler, validsampler


def train(dataloader, model, optimizer, criterion, evaluator, epoch, args):
    # switch to train mode
    model.train()

    tracker = utils.EpochTracker()

    for idx, batch in enumerate(dataloader):
        inputs, targets = batch
        inputs, targets = inputs.to(args.device, non_blocking=True), targets.to(args.device, non_blocking=True)

        outputs = model(inputs)
        loss = criterion(outputs, targets)
        acc1, acc5 = evaluator(outputs, targets)

        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()

        header = f'Epoch[{epoch + 1:{len(str(args.epochs))}d}/{args.epochs}] - ' \
                 f'batch[{idx + 1:{len(str(len(dataloader)))}d}/{len(dataloader)}]'
        tracker.update(loss=loss.item())
        tracker.update(acc1=acc1.item())
        tracker.update(acc5=acc5.item())
        tracker.display(header=header)

        if args.writer:
            global_steps = epoch * len(dataloader) + idx
            args.writer.add_scalar('train / loss (batch)', tracker.trackers['loss'].value, global_steps)
            args.writer.add_scalar('train / acc1 (batch)', tracker.trackers['acc1'].value, global_steps)
            args.writer.add_scalar('train / acc5 (batch)', tracker.trackers['acc5'].value, global_steps)

    header = f'Epoch[{epoch + 1:{len(str(args.epochs))}d}/{args.epochs}]'
    tracker.synchronize_between_processes()
    tracker.summarize(header=header)

    if args.writer:
        global_steps = epoch
        args.writer.add_scalar('train / loss (epoch)', tracker.trackers['loss'].global_average, global_steps)
        args.writer.add_scalar('train / acc1 (epoch)', tracker.trackers['acc1'].global_average, global_steps)
        args.writer.add_scalar('train / acc5 (epoch)', tracker.trackers['acc5'].global_average, global_steps)


@torch.no_grad()
def valid(dataloader, model, criterion, evaluator, epoch, args):
    # switch to eval mode
    model.eval()

    tracker = utils.EpochTracker()

    for idx, batch in enumerate(dataloader):
        inputs, targets = batch
        inputs, targets = inputs.to(args.device, non_blocking=True), targets.to(args.device, non_blocking=True)

        outputs = model(inputs)
        loss = criterion(outputs, targets)
        acc1, acc5 = evaluator(outputs, targets)

        header = f'Epoch[{epoch + 1:{len(str(args.epochs))}d}/{args.epochs}] - ' \
                 f'batch[{idx + 1:{len(str(len(dataloader)))}d}/{len(dataloader)}]'
        tracker.update(loss=loss.item())
        tracker.update(acc1=acc1.item())
        tracker.update(acc5=acc5.item())
        tracker.display(header=header)

        if args.writer:
            global_steps = epoch * len(dataloader) + idx
            args.writer.add_scalar('valid / loss (batch)', tracker.trackers['loss'].value, global_steps)
            args.writer.add_scalar('valid / acc1 (batch)', tracker.trackers['acc1'].value, global_steps)
            args.writer.add_scalar('valid / acc5 (batch)', tracker.trackers['acc5'].value, global_steps)

    header = f'Epoch[{epoch + 1:{len(str(args.epochs))}d}/{args.epochs}]'
    tracker.synchronize_between_processes()
    tracker.summarize(header=header)

    if args.writer:
        global_steps = epoch
        args.writer.add_scalar('valid / loss (epoch)', tracker.trackers['loss'].global_average, global_steps)
        args.writer.add_scalar('valid / acc1 (epoch)', tracker.trackers['acc1'].global_average, global_steps)
        args.writer.add_scalar('valid / acc5 (epoch)', tracker.trackers['acc5'].global_average, global_steps)

    return tracker.trackers['acc1'].global_average


if __name__ == '__main__':
    session = init_parser()
    arguments = session.parse_args()
    main(arguments)
