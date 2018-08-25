import argparse
import sys
import os
import numpy as np
import torch

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

import lib.toy_data as toy_data
import lib.utils as utils

from train_misc import standard_normal_logprob, count_parameters
from train_misc import set_cnf_options, add_spectral_norm, create_regularization_fns
from train_misc import build_model_toy2d

parser = argparse.ArgumentParser()
parser.add_argument('--checkpt', type=str, required=True)
parser.add_argument('--ntimes', type=int, default=101)
parser.add_argument('--memory', type=float, default=0.01, help='Higher this number, the more memory is consumed.')
parser.add_argument('--save', type=str, default='trajectory')
args = parser.parse_args()

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

# Load checkpoint.
checkpt = torch.load(args.checkpt, map_location=lambda storage, loc: storage)
ckpt_args = checkpt['args']
state_dict = checkpt['state_dict']

# Construct model and restore checkpoint.
regularization_fns, regularization_coeffs = create_regularization_fns(ckpt_args)
model = build_model_toy2d(ckpt_args, regularization_fns).to(device)
if ckpt_args.spectral_norm: add_spectral_norm(model)
set_cnf_options(ckpt_args, model)

model.load_state_dict(state_dict)
model.to(device)

print(model)
print("Number of trainable parameters: {}".format(count_parameters(model)))

# Load samples from dataset
data_samples = toy_data.inf_train_gen(ckpt_args.data, batch_size=2000)

#  Sample from prior
z_samples = torch.randn(2000, 2).to(device)

# sample from a grid
npts = 800
side = np.linspace(-4, 4, npts)
xx, yy = np.meshgrid(side, side)
xx = torch.from_numpy(xx).type(torch.float32).to(device)
yy = torch.from_numpy(yy).type(torch.float32).to(device)
z_grid = torch.cat([xx.reshape(-1, 1), yy.reshape(-1, 1)], 1)

with torch.no_grad():
    # We expect the model is a chain of CNF layers wrapped in a SequentialFlow container.
    logp_samples = torch.sum(standard_normal_logprob(z_samples), 1, keepdim=True)
    logp_grid = torch.sum(standard_normal_logprob(z_grid), 1, keepdim=True)
    tt = 0
    for cnf in model.chain:
        end_time = (cnf.sqrt_end_time * cnf.sqrt_end_time)
        integration_times = torch.linspace(0, end_time, args.ntimes)

        z_traj, _ = cnf(z_samples, logp_samples, integration_times=integration_times, reverse=True)
        z_traj = z_traj.cpu().numpy()

        grid_z_traj, grid_logpz_traj = [], []
        inds = torch.arange(0, z_grid.shape[0]).to(torch.int64)
        for ii in torch.split(inds, int(z_grid.shape[0] * args.memory)):
            _grid_z_traj, _grid_logpz_traj = cnf(
                z_grid[ii], logp_grid[ii], integration_times=integration_times, reverse=True
            )
            _grid_z_traj, _grid_logpz_traj = _grid_z_traj.cpu().numpy(), _grid_logpz_traj.cpu().numpy()
            grid_z_traj.append(_grid_z_traj)
            grid_logpz_traj.append(_grid_logpz_traj)
        grid_z_traj = np.concatenate(grid_z_traj, axis=1)
        grid_logpz_traj = np.concatenate(grid_logpz_traj, axis=1)

        plt.figure(figsize=(8, 8))
        for t in range(z_traj.shape[0]):

            plt.clf()

            # plot target potential function
            ax = plt.subplot(2, 2, 1, aspect="equal")

            ax.hist2d(data_samples[:, 0], data_samples[:, 1], range=[[-4, 4], [-4, 4]], bins=200)
            ax.invert_yaxis()
            ax.get_xaxis().set_ticks([])
            ax.get_yaxis().set_ticks([])
            ax.set_title("Target", fontsize=32)

            # plot the density
            ax = plt.subplot(2, 2, 2, aspect="equal")

            z, logqz = grid_z_traj[t], grid_logpz_traj[t]

            xx = z[:, 0].reshape(npts, npts)
            yy = z[:, 1].reshape(npts, npts)
            qz = np.exp(logqz).reshape(npts, npts)

            plt.pcolormesh(xx, yy, qz)
            ax.set_xlim(-4, 4)
            ax.set_ylim(-4, 4)
            cmap = matplotlib.cm.get_cmap(None)
            ax.set_axis_bgcolor(cmap(0.))
            ax.invert_yaxis()
            ax.get_xaxis().set_ticks([])
            ax.get_yaxis().set_ticks([])
            ax.set_title("Density", fontsize=32)

            # plot the samples
            ax = plt.subplot(2, 2, 3, aspect="equal")

            zk = z_traj[t]
            ax.hist2d(zk[:, 0], zk[:, 1], range=[[-4, 4], [-4, 4]], bins=200)
            ax.invert_yaxis()
            ax.get_xaxis().set_ticks([])
            ax.get_yaxis().set_ticks([])
            ax.set_title("Samples", fontsize=32)

            # plot vector field
            ax = plt.subplot(2, 2, 4, aspect="equal")

            K = 13j
            y, x = np.mgrid[-4:4:K, -4:4:K]
            K = int(K.imag)
            zs = torch.from_numpy(np.stack([x, y], -1).reshape(K * K, 2)).to(device, torch.float32)
            logps = torch.zeros(zs.shape[0], 1).to(device, torch.float32)
            dydt = cnf.odefunc(integration_times[t], (zs, logps))[0]
            dydt = dydt.cpu().numpy()
            dydt = dydt.reshape(K, K, 2)

            logmag = 2 * np.log(np.hypot(dydt[:, :, 0], dydt[:, :, 1]))
            ax.quiver(
                x, y, dydt[:, :, 0], dydt[:, :, 1], np.exp(logmag), cmap="coolwarm", scale=10., width=0.015, pivot="mid"
            )
            ax.set_xlim(-4, 4)
            ax.set_ylim(-4, 4)
            ax.axis("off")
            ax.set_title("Vector Field", fontsize=32)

            utils.makedirs(args.save)
            plt.savefig(os.path.join(args.save, f"viz-{t:05d}.jpg"))