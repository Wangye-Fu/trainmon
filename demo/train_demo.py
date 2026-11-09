#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A deliberately small MLP trainer used to debug trainmon.

It uses only synthetic data, so it works on an offline GPU server.
"""

import argparse
import logging
import os
import time

import torch
from torch import nn

try:
    import trainmon
except ImportError:
    trainmon = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class MLP(nn.Module):
    def __init__(self):
        super(MLP, self).__init__()
        self.net = nn.Sequential(
            nn.Linear(20, 64),
            nn.ReLU(),
            nn.Linear(64, 3),
        )

    def forward(self, x):
        return self.net(x)


def make_dataset(size, device):
    x = torch.randn(size, 20)
    w1 = torch.randn(20, 32)
    w2 = torch.randn(32, 3)
    logits = torch.relu(x.matmul(w1)).matmul(w2)
    y = logits.argmax(dim=1)
    return x.to(device), y.to(device)


def evaluate(model, criterion, x, y):
    model.eval()
    with torch.no_grad():
        logits = model(x)
        loss = criterion(logits, y)
        acc = (logits.argmax(dim=1) == y).float().mean()
    model.train()
    return float(loss.item()), float(acc.item())


def build_args():
    parser = argparse.ArgumentParser(description="tiny synthetic MLP trainer")
    parser.add_argument("--steps", type=int, default=1200)
    parser.add_argument("--batch", type=int, default=128)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--sleep", type=float, default=0.05,
                        help="sleep per step to make the demo observable")
    parser.add_argument("--eval-every", type=int, default=200)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--run", default="demo-mlp")
    parser.add_argument("--log", default=None,
                        help="default: logs/train-<timestamp>.log (unique per run)")
    parser.add_argument("--no-report", action="store_true",
                        help="only write logging lines, do not call trainmon.report")
    parser.add_argument("--fail-at", type=int, default=0,
                        help="raise a simulated failure at this step")
    parser.add_argument("--stall-at", type=int, default=0,
                        help="sleep without progress at this step")
    parser.add_argument("--stall-for", type=float, default=40.0)
    parser.add_argument("--stall-threshold", type=float, default=30.0,
                        help="trainmon stall detection threshold")
    return parser.parse_args()


def setup_logging(path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    return path


def main():
    args = build_args()
    if not args.log:
        args.log = os.path.join(
            BASE_DIR, "logs", "train-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
    log_path = setup_logging(args.log)
    if args.device == "cuda":
        device = torch.device("cuda")
    elif args.device == "cpu":
        device = torch.device("cpu")
    else:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    torch.manual_seed(42)
    train_x, train_y = make_dataset(8192, device)
    val_x, val_y = make_dataset(2048, device)
    model = MLP().to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, args.steps))

    logging.info("device=%s", device)
    if device.type == "cuda":
        logging.info("gpu=%s", torch.cuda.get_device_name(0))
    logging.info("starting training: steps=%d batch=%d log=%s",
                 args.steps, args.batch, log_path)

    if trainmon is not None:
        trainmon.attach(
            run=args.run,
            log=log_path,
            total_steps=args.steps,
            stall_seconds=args.stall_threshold,
        )
    else:
        logging.warning("trainmon is not installed; monitoring disabled")

    started = time.time()
    last_loss = None
    for step in range(1, args.steps + 1):
        if args.fail_at and step == args.fail_at:
            message = "simulated CUDA out of memory at step %d" % step
            logging.error(message)
            raise RuntimeError(message)

        indices = torch.randint(0, train_x.size(0), (args.batch,), device=device)
        optimizer.zero_grad()
        logits = model(train_x[indices])
        loss = criterion(logits, train_y[indices])
        loss.backward()
        optimizer.step()
        scheduler.step()

        last_loss = float(loss.item())
        lr = float(optimizer.param_groups[0]["lr"])
        if trainmon is not None and not args.no_report:
            trainmon.report(
                step=step,
                loss=last_loss,
                lr=lr,
                epoch=step * 10.0 / args.steps,
                total_steps=args.steps,
            )

        if step % 10 == 0 or step == 1:
            elapsed = max(1e-6, time.time() - started)
            logging.info(
                "step %d/%d loss=%.4f lr=%.2e speed=%.1f step/s",
                step, args.steps, last_loss, lr, step / elapsed)

        if args.eval_every and step % args.eval_every == 0:
            val_loss, val_acc = evaluate(model, criterion, val_x, val_y)
            logging.info(
                "eval step=%d/%d val_loss=%.4f val_acc=%.4f",
                step, args.steps, val_loss, val_acc)

        if args.stall_at and step == args.stall_at:
            logging.warning("simulating a %.0fs stall (no new progress)",
                            args.stall_for)
            time.sleep(args.stall_for)

        if args.sleep > 0:
            time.sleep(args.sleep)

    val_loss, val_acc = evaluate(model, criterion, val_x, val_y)
    logging.info("Training complete: final val_loss=%.4f val_acc=%.4f",
                 val_loss, val_acc)
    if trainmon is not None:
        trainmon.finish("completed", 0)
    logging.info("monitor demo finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.exception("training failed: %s", exc)
        if trainmon is not None:
            trainmon.finish("failed", 1, "%s: %s" % (type(exc).__name__, exc))
        raise
