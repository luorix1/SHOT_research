import os
os.environ['OMP_NUM_THREADS'] = '1'
import argparse
import sys
import shutil
from distutils.dir_util import copy_tree
import datetime
import numpy as np
import random
import torch
import torch.optim as optim
import torchvision.transforms as T
from multiview_detector.datasets import *
from multiview_detector.loss.gaussian_mse import GaussianMSE
from multiview_detector.models.self_res_dpersp_trans_detector import SRDPerspTransDetector
from multiview_detector.models.res_dpersp_trans_detector import RDPerspTransDetector
# from multiview_detector.models.intrinsic_dpersp_trans_detector import IDPerspTransDetector
from multiview_detector.models.dpersp_trans_detector import DPerspTransDetector
from multiview_detector.models.persp_trans_detector import PerspTransDetector
from multiview_detector.models.image_proj_variant import ImageProjVariant
from multiview_detector.models.res_proj_variant import ResProjVariant
from multiview_detector.models.no_joint_conv_variant import NoJointConvVariant
from multiview_detector.utils.logger import Logger
from multiview_detector.utils.draw_curve import draw_curve
from multiview_detector.utils.image_utils import img_color_denormalize
from multiview_detector.trainer import PerspectiveTrainer
from multiview_detector.d_trainer import DPerspectiveTrainer


# Credit: https://github.com/sberbank-ai/ru-dalle/blob/e96631a867fcadcfaa52eecb20b1e42b88aa4386/rudalle/utils.py
def seed_everything(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

def main(args):
    # seed
    if args.seed is not None:
        seed_everything(args.seed)
    else:
        torch.backends.cudnn.benchmark = True

    # dataset
    normalize = T.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    denormalize = img_color_denormalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    train_trans = T.Compose([T.Resize([720, 1280]), T.ToTensor(), normalize, ])
    # change the following directories to fit your needs
    if 'wildtrack' in args.dataset:
        data_path = '/data/Data/Wildtrack'
        base = Wildtrack(data_path)
    elif 'multiviewx' in args.dataset:
        data_path = '/data/Data/MultiviewX'
        base = MultiviewX(data_path)
    else:
        raise Exception('must choose from [wildtrack, multiviewx]')
    train_set = frameDataset(base, train=True, transform=train_trans, grid_reduce=4)
    test_set = frameDataset(base, train=False, transform=train_trans, grid_reduce=4)
    # test_set = frameDataset(base, train=False, transform=train_trans, grid_reduce=4, train_ratio=0.05)

    train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True,
                                               num_workers=args.num_workers, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(test_set, batch_size=args.batch_size, shuffle=False,
                                              num_workers=args.num_workers, pin_memory=True)

    # model
    if args.variant == 'default':
        model = DPerspTransDetector(train_set, args.arch, args.depth_scales)
    elif args.variant == 'self':
        model = SRDPerspTransDetector(train_set, args.arch, args.depth_scales, args.use_GN)
    elif args.variant == 'custom':
        model = RDPerspTransDetector(train_set, args.arch, args.depth_scales, args.use_local, args.use_global, args.use_GN, args.use_SSM)
        # model = IDPerspTransDetector(train_set, args.arch, args.depth_scales)
    elif args.variant == 'per':
        model = PerspTransDetector(train_set, args.arch)
    elif args.variant == 'img_proj':
        model = ImageProjVariant(train_set, args.arch)
    elif args.variant == 'res_proj':
        model = ResProjVariant(train_set, args.arch)
    elif args.variant == 'no_joint_conv':
        model = NoJointConvVariant(train_set, args.arch)
    else:
        raise Exception('no support for this variant')
    if args.load is not None:
        model.load_state_dict(torch.load(args.load))
        print(f'{args.load} loaded')

    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=args.lr, steps_per_epoch=len(train_loader),
                                                    epochs=args.epochs)

    # loss
    criterion = GaussianMSE().cuda()

    # logging
    logdir = args.logdir
    if logdir is None:
        logdir = f'logs/{args.dataset}_frame/{args.variant}/{args.loginfo}' + datetime.datetime.today().strftime('%Y-%m-%d_%H-%M-%S') \
            if not args.resume else f'logs/{args.dataset}_frame/{args.variant}/{args.resume}'
    if args.resume is None:
        os.makedirs(logdir, exist_ok=True)
        copy_tree('./multiview_detector', logdir + '/scripts/multiview_detector')
        for script in os.listdir('.'):
            if script.split('.')[-1] == 'py':
                dst_file = os.path.join(logdir, 'scripts', os.path.basename(script))
                shutil.copyfile(script, dst_file)
        sys.stdout = Logger(os.path.join(logdir, 'log.txt'), )
    print('Settings:')
    print(vars(args))

    # draw curve
    x_epoch = []
    train_loss_s = []
    train_prec_s = []
    test_loss_s = []
    test_prec_s = []
    test_moda_s = []

    if args.variant == 'default' or 'custom' or 'self':
        trainer = DPerspectiveTrainer(model, criterion, logdir, denormalize, args, args.cls_thres, args.alpha)
    else:
        trainer = PerspectiveTrainer(model, criterion, logdir, denormalize, args.cls_thres, args.alpha)

    # learn
    if args.resume is None:
        # print('Testing...')
        trainer.test(test_loader, os.path.join(logdir, 'test.txt'), train_set.gt_fpath, False)

        for epoch in range(1, args.epochs + 1):
            print('Training...')
            train_loss, train_prec = trainer.train(epoch, train_loader, optimizer, args.log_interval, scheduler)
            print('Testing...')
            test_loss, test_prec, moda = trainer.test(test_loader, os.path.join(logdir, 'test.txt'),
                                                      train_set.gt_fpath, False)

            x_epoch.append(epoch)
            train_loss_s.append(train_loss)
            train_prec_s.append(train_prec)
            test_loss_s.append(test_loss)
            test_prec_s.append(test_prec)
            test_moda_s.append(moda)
            draw_curve(os.path.join(logdir, 'learning_curve.jpg'), x_epoch, train_loss_s, train_prec_s,
                       test_loss_s, test_prec_s, test_moda_s)
            # save
            torch.save(model.state_dict(), os.path.join(logdir, 'MultiviewDetector.pth'))
    else:
        resume_dir = f'logs/{args.dataset}_frame/{args.variant}/' + args.resume
        resume_fname = resume_dir + '/MultiviewDetector.pth'
        model.load_state_dict(torch.load(resume_fname))
        model.eval()
    print('Test loaded model...')
    trainer.test(test_loader, os.path.join(logdir, 'test.txt'), train_set.gt_fpath, False)


if __name__ == '__main__':
    # settings
    parser = argparse.ArgumentParser(description='Multiview detector')
    parser.add_argument('--reID', action='store_true')
    parser.add_argument('--cls_thres', type=float, default=0.4)
    parser.add_argument('--alpha', type=float, default=1.0, help='ratio for per view loss')
    parser.add_argument('--variant', type=str, default='default',
                        choices=['default', 'custom', 'self', 'per', 'img_proj', 'res_proj', 'no_joint_conv'])
    parser.add_argument('--arch', type=str, default='resnet18', choices=['vgg11', 'resnet18'])
    parser.add_argument('-d', '--dataset', type=str, default='wildtrack', choices=['wildtrack', 'multiviewx'])
    parser.add_argument('-j', '--num_workers', type=int, default=4)
    parser.add_argument('-b', '--batch_size', type=int, default=1, metavar='N',
                        help='input batch size for training (default: 1)')
    parser.add_argument('--epochs', type=int, default=10, metavar='N', help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.15, metavar='LR', help='learning rate (default: 0.1)')
    parser.add_argument('--weight_decay', type=float, default=5e-4)
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M', help='SGD momentum (default: 0.5)')
    parser.add_argument('--depth_scales', type=int, default=5)
    parser.add_argument('--log_interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--resume', type=str, default=None)
    parser.add_argument('--visualize', action='store_true')
    parser.add_argument('--seed', type=int, default=1, help='random seed (default: None)')
    parser.add_argument('--loginfo', type=str, default='')
    parser.add_argument('--logdir', type=str, default=None)
    parser.add_argument('--load', type=str, default=None)
    parser.add_argument('--no_matlab', type=int, default=0)
    parser.add_argument('--use_local', action='store_true')
    parser.add_argument('--use_global', action='store_true')
    parser.add_argument('--use_GN', action='store_true')
    parser.add_argument('--use_SSM', action='store_true')
    args = parser.parse_args()

    main(args)
