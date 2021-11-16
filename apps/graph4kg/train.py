# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import time
import warnings
from collections import defaultdict

import paddle
import numpy as np
import paddle.distributed as dist

from dataset.reader import read_trigraph
from dataset.dataset import create_dataloaders
from models.ke_model import KGEModel
from models.loss_func import LossFunction
from utils import set_seed, set_logger, print_log
from utils import evaluate
from config import prepare_config


def main():
    """Main function for shallow knowledge embedding methods
    """
    args = prepare_config()
    set_seed(args.seed)
    set_logger(args)

    trigraph = read_trigraph(args.data_path, args.data_name)
    if args.data_name == 'wikikg90m':
        trigraph.sampled_subgraph(0.01, dataset='valid')

    use_filter_set = args.filter_sample or args.filter_eval or args.sample_weight
    if use_filter_set:
        filter_dict = {
            'head': trigraph.true_heads_for_tail_rel,
            'tail': trigraph.true_tails_for_head_rel
        }
    else:
        filter_dict = None

    if dist.get_world_size() > 1:
        dist.init_parallel_env()

    model = KGEModel(args.model_name, trigraph, args)

    if args.async_update:
        model.start_async_update()

    if dist.get_world_size() > 1 and len(model.parameters()) > 0:
        model = paddle.DataParallel(model)
        model = model._layers

    if len(model.parameters()) > 0:
        if args.mlp_optimizer == 'Adam':
            optim_func = paddle.optimizer.Adam
        elif args.mlp_optimizer == 'Adagrad':
            optim_func = paddle.optimizer.Adagrad
        else:
            errors = 'optimizer {} not supported!'.format(args.mlp_optimizer)
            raise ValueError(errors)
        optimizer = optim_func(
            learning_rate=args.mlp_lr,
            epsilon=1e-10,
            parameters=model.parameters())
    else:
        warnings.warn('there is no model parameter on gpu, optimizer is None.',
                      RuntimeWarning)
        optimizer = None

    loss_func = LossFunction(
        name=args.loss_type,
        pairwise=args.pairwise,
        margin=args.margin,
        neg_adv_spl=args.neg_adversarial_sampling,
        neg_adv_temp=args.adversarial_temperature)

    train_loader, valid_loader, test_loader = create_dataloaders(
        trigraph,
        args,
        filter_dict=filter_dict if use_filter_set else None,
        shared_ent_path=model.shared_ent_path if args.mix_cpu_gpu else None)

    timer = defaultdict(int)
    log = defaultdict(int)
    ts = t_step = time.time()
    step = 1
    for epoch in range(args.num_epoch):
        model.train()
        for indexes, prefetch_embeddings, mode in train_loader:
            h, r, t, neg_ents, all_ents = indexes
            all_ents_emb, rel_emb, weights = prefetch_embeddings

            if rel_emb is not None:
                rel_emb.stop_gradient = False
            if all_ents_emb is not None:
                all_ents_emb.stop_gradient = False

            timer['sample'] += (time.time() - ts)

            ts = time.time()
            h_emb, r_emb, t_emb, neg_emb, mask = model.prepare_inputs(
                h, r, t, all_ents, neg_ents, all_ents_emb, rel_emb, mode, args)
            pos_score = model.forward(h_emb, r_emb, t_emb)

            if mode == 'head':
                neg_score = model.get_neg_score(t_emb, r_emb, neg_emb, True,
                                                mask)
            elif mode == 'tail':
                neg_score = model.get_neg_score(h_emb, r_emb, neg_emb, False,
                                                mask)
            else:
                raise ValueError('unsupported negative mode {}'.format(mode))
            neg_score = neg_score.reshape([args.batch_size, -1])

            loss = loss_func(pos_score, neg_score, weights)
            log['loss'] += loss.numpy()[0]

            if args.use_embedding_regularization:
                reg_loss = model.get_regularization(h_emb, r_emb, t_emb,
                                                    neg_emb)
                log['reg'] += reg_loss.numpy()[0]

                loss = loss + reg_loss
            timer['forward'] += (time.time() - ts)

            ts = time.time()
            loss.backward()
            timer['backward'] += (time.time() - ts)

            ts = time.time()
            if optimizer is not None:
                optimizer.step()
                optimizer.clear_grad()

            if args.mix_cpu_gpu:
                ent_trace, rel_trace = model.create_trace(
                    all_ents, all_ents_emb, r, r_emb)
                model.step(ent_trace, rel_trace)
            else:
                model.step()

            timer['update'] += (time.time() - ts)

            if (step + 1) % args.log_interval == 0:
                print_log(step, args.log_interval, log, timer, t_step)
                timer = defaultdict(int)
                log = defaultdict(int)
                t_step = time.time()

            if args.valid and (step + 1) % args.eval_interval == 0:
                evaluate(model, valid_loader, 'valid', filter_dict
                         if args.filter_eval else None, data_mode=args.data_name)

            step += 1
            if step % args.save_interval == 0 or step > args.max_steps:
                step_path = os.path.join(args.save_path, 'step_%s' % step)
                model.save(step_path)
            if step > args.max_steps and args.test:
                evaluate(model, test_loader, 'test', filter_dict if args.filter_eval else None,
                         data_mode=args.data_name)
                break

            ts = time.time()

    if args.async_update:
        model.finish_async_update()

    if args.test:
        evaluate(model, test_loader, 'test', filter_dict
                 if args.filter_eval else None,
                 os.path.join(args.save_path, 'test.pkl'), data_mode=args.data_name)


if __name__ == '__main__':
    main()
