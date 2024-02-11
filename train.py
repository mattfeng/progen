from dotenv import load_dotenv
load_dotenv()

import click
import humanize
from jinja2 import Template
from pathlib import Path
import tqdm
import numpy as np

import toml

import jax
from jax import nn, random, jit, tree_util, tree_map
from optax import adamw, clip_by_global_norm, chain, apply_updates, apply_every

from haiku import PRNGSequence

from progen_transformer import ProGen
from progen_transformer.data import decode_tokens, iterator_from_tfrecords_folder
from progen_transformer.utils import sample, get_loss_fn, set_hardware_rng_, confirm, exists
from progen_transformer.checkpoint import get_checkpoint_fns

import wandb

# sample html

sample_tmpl = Template("""<i>{{prime_str}}</i><br/><br/><div style="overflow-wrap: break-word;">{{sampled_str}}</div>""")

# speedup rng

set_hardware_rng_(jax)

# main functions

@click.command()
@click.option('--seed', default = 42)
@click.option('--batch_size', default = 4)
@click.option('--grad_accum_every', default = 4)
@click.option('--epochs', default = 100)
@click.option('--learning_rate', default = 2e-4)
@click.option('--weight_decay', default = 1e-3)
@click.option('--data_parallel', default = False, is_flag = True)
@click.option('--max_grad_norm', default = 0.5)
@click.option('--validate_every', default = 100)
@click.option('--sample_every', default = 500)
@click.option('--checkpoint_every', default = 1000)
@click.option('--checkpoint_path', default = './ckpts')
@click.option('--checkpoint_keep_n', default = 500)
@click.option('--config_path', default = './configs/model')
@click.option('--model_name', default = 'default')
@click.option('--prime_length', default = 25)
@click.option('--seq_len', default = 1024)
@click.option('--mixed_precision', default = False, is_flag = True)
@click.option('--data_path', default = './train_data')
@click.option('--wandb_off', default = False, is_flag = True)
@click.option('--wandb_project_name', default = 'progen-training')
@click.option('--new', default = False, is_flag = True)
def main(
    seed,
    batch_size,
    grad_accum_every,
    epochs,
    learning_rate,
    weight_decay,
    data_parallel,
    max_grad_norm,
    validate_every,
    sample_every,
    checkpoint_every,
    checkpoint_path,
    checkpoint_keep_n,
    config_path,
    model_name,
    prime_length,
    seq_len,
    mixed_precision,
    data_path,
    wandb_off,
    wandb_project_name,
    new
):
    # prepare folders

    reset_checkpoint, get_last_checkpoint, save_checkpoint = get_checkpoint_fns(checkpoint_path)

    if new:
        if not confirm('are you sure you want to clear all your checkpoints and restart training?'):
            exit()
        reset_checkpoint()

    # initialize all states, or load from checkpoint

    last_checkpoint = get_last_checkpoint()

    if not exists(last_checkpoint):
        config_folder_path = Path(config_path)
        config_path = config_folder_path / f'{model_name}.toml'
        assert config_path.exists(), f'path to your model config {str(config_path)} does not exist'
        model_kwargs = toml.loads(config_path.read_text())
    else:
        model_kwargs = last_checkpoint['model_config']

    # setup model and params

    model = ProGen(**{
        **model_kwargs,
        'mixed_precision': mixed_precision
    })

    model_apply = jit(model.apply)
    rng = PRNGSequence(seed)
    loss_fn = get_loss_fn(model, data_parallel = data_parallel)

    # optimizer

    exclude_norm_and_bias_params = lambda p: tree_map(lambda x: x.ndim > 1, p)

    optim = chain(
        clip_by_global_norm(max_grad_norm),
        adamw(learning_rate, weight_decay = weight_decay, mask = exclude_norm_and_bias_params),
        apply_every(grad_accum_every)
    )

    # get params and optimizer state

    if exists(last_checkpoint):
        params = last_checkpoint['params']
        optim_state = last_checkpoint['optim_state']
        start_seq_index = last_checkpoint['next_seq_index']
    else:
        mock_data = np.zeros((model_kwargs['seq_len'],), dtype = np.uint8)
        params = model.init(next(rng), mock_data)
        optim_state = optim.init(params)
        start_seq_index = 0

    # experiment tracker

    seq_len = model_kwargs['seq_len']
    num_params = tree_util.tree_reduce(lambda acc, el: acc + el.size, params, 0)
    num_params_readable = humanize.naturalsize(num_params)

    wandb.config.num_params = num_params

    wandb_kwargs = {'mode': 'disabled'} if wandb_off else {}

    if exists(last_checkpoint) and exists(last_checkpoint['run_id']):
        run_id = last_checkpoint['run_id']
        wandb_kwargs = {**wandb_kwargs, 'id': run_id, 'resume': 'allow'}

    wandb.init(project = wandb_project_name, **wandb_kwargs)
    wandb_run_id = wandb.run.id if not wandb_off else None

    # get tf dataset

    total_train_seqs, get_train_dataset = iterator_from_tfrecords_folder(data_path, data_type = 'train')
    total_valid_seqs, get_valid_dataset = iterator_from_tfrecords_folder(data_path, data_type = 'valid')

    assert total_train_seqs > 0, 'no protein sequences found for training'
    assert total_valid_seqs > 0, 'no protein sequences found for validation'

    train_dataset = get_train_dataset(
        seq_len=seq_len,
        batch_size=batch_size,
        skip=start_seq_index, 
        loop=True
    ) # note that because loop=True, batches on different epochs will be different

    valid_dataset = get_valid_dataset(
        seq_len=seq_len,
        batch_size=batch_size,
        loop=True
    )

    # print

    print(f'params: {num_params_readable}')
    print(f'sequence length: {seq_len}')
    print(f'num sequences: {total_train_seqs}')
    print(f'starting from sequence {start_seq_index}')

    # training

    effective_batch_size = batch_size * grad_accum_every
    seq_index_ranges = range(start_seq_index, total_train_seqs, effective_batch_size)    

    for epoch in range(1, epochs + 1):
        print(f"==== starting epoch: {epoch} ====")

        for i, seq_index in tqdm.tqdm(enumerate(seq_index_ranges), mininterval = 10., desc = 'training', total = len(seq_index_ranges)):
            for _ in range(grad_accum_every):
                data = next(train_dataset)

                loss, grads = loss_fn(params, next(rng), data)
                updates, optim_state = optim.update(grads, optim_state, params)
                params = apply_updates(params, updates)

            print(f'loss: {loss.item()}')
            wandb.log({'loss': loss.item()})

            if i % checkpoint_every == 0:
                package = {
                    'next_seq_index': seq_index + effective_batch_size,
                    'params': params,
                    'optim_state': optim_state,
                    'model_config': model_kwargs,
                    'run_id': wandb_run_id
                }

                save_checkpoint(package, checkpoint_keep_n)
                print(f"checkpoint to start at sequence index of {package['next_seq_index']}")

            if i % validate_every == 0:
                valid_data = next(valid_dataset)
                loss, _ = loss_fn(params, next(rng), valid_data)
                print(f'valid_loss: {loss.item()}')
                wandb.log({'valid_loss': loss.item()})

            if i % sample_every == 0:
                valid_data = next(valid_dataset)[0]
                prime = valid_data[:prime_length]
                prime_str = decode_tokens(prime)

                sampled = sample(rng, model_apply, params, prime, seq_len, top_k = 25)
                sampled_str = decode_tokens(sampled[prime_length:])

                print(prime_str, "\n", "*" * 40, "\n", sampled_str)
                wandb.log({'samples': wandb.Html(sample_tmpl.render(prime_str = prime_str, sampled_str = sampled_str))})

if __name__ == '__main__':
    main()
