# Copyright 2024 DeepMind Technologies Limited
#
# AlphaFold 3 source code is licensed under CC BY-NC-SA 4.0. To view a copy of
# this license, visit https://creativecommons.org/licenses/by-nc-sa/4.0/
#
# To request access to the AlphaFold 3 model parameters, follow the process set
# out at https://github.com/google-deepmind/alphafold3. You may only use these
# if received directly from Google. Use is subject to terms of use available at
# https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md

"""Specialized mapping functions."""

from collections.abc import Callable, Sequence
import functools
from typing import Any, TypeVar

import haiku as hk
import jax
import jax.numpy as jnp


Pytree = Any
PytreeJaxArray = Any

partial = functools.partial
PROXY = object()

T = TypeVar("T")


def _maybe_slice(array, i, slice_size, axis):
  if axis is PROXY:
    return array
  else:
    return jax.lax.dynamic_slice_in_dim(
        array, i, slice_size=slice_size, axis=axis
    )


def _maybe_get_size(array, axis):
  if axis == PROXY:
    return -1
  else:
    return array.shape[axis]


def _expand_axes(axes, values, name="sharded_apply"):
  values_tree_def = jax.tree_util.tree_structure(values)
  flat_axes = jax.api_util.flatten_axes(name, values_tree_def, axes)
  # Replace None's with PROXY.
  flat_axes = [PROXY if x is None else x for x in flat_axes]
  return jax.tree_util.tree_unflatten(values_tree_def, flat_axes)


def sharded_map(
    fun: Callable[..., PytreeJaxArray],
    shard_size: int | None = 1,
    in_axes: int | Pytree = 0,
    out_axes: int | Pytree = 0,
) -> Callable[..., PytreeJaxArray]:
  """Sharded vmap.

  Maps `fun` over axes, in a way similar to hk.vmap, but does so in shards of
  `shard_size`. This allows a smooth trade-off between memory usage
  (as in a plain map) vs higher throughput (as in a vmap).

  Args:
    fun: Function to apply smap transform to.
    shard_size: Integer denoting shard size.
    in_axes: Either integer or pytree describing which axis to map over for each
      input to `fun`, None denotes broadcasting.
    out_axes: Integer or pytree denoting to what axis in the output the mapped
      over axis maps.

  Returns:
    Function with smap applied.
  """
  if hk.running_init():
    # Guarantees initialisation independent of shard_size. Doesn't incur a high
    # memory cost, as long as large concrete tensors are not encountered.
    return hk.vmap(fun, in_axes=in_axes, out_axes=out_axes, split_rng=False)
  else:
    vmapped_fun = hk.vmap(fun, in_axes, out_axes, split_rng=True)
    return sharded_apply(vmapped_fun, shard_size, in_axes, out_axes)


def _set_docstring(docstr: str) -> Callable[[T], T]:
  """Decorator for setting the docstring of a function."""

  def wrapped(fun: T) -> T:
    fun.__doc__ = docstr.format(fun=getattr(fun, "__name__", repr(fun)))
    return fun

  return wrapped


def sharded_apply(
    fun: Callable[..., PytreeJaxArray],
    shard_size: int | None = 1,
    in_axes: int | Pytree = 0,
    out_axes: int | Pytree = 0,
    new_out_axes: bool = False,
) -> Callable[..., PytreeJaxArray]:
  """Sharded apply.

  Applies `fun` over shards to axes, in a way similar to vmap,
  but does so in shards of `shard_size`. Shards are stacked after.
  This allows a smooth trade-off between
  memory usage (as in a plain map) vs higher throughput (as in a vmap).

  Args:
    fun: Function to apply smap transform to.
    shard_size: Integer denoting shard size. None will return `fun` unchanged.
    in_axes: Either integer or pytree describing which axis to map over for each
      input to `fun`, None denotes broadcasting.
    out_axes: Integer or pytree denoting to what axis in the output the mapped
      over axis maps.
    new_out_axes: Whether to stack outputs on new axes. This assumes that the
      output sizes for each shard (including the possible remainder shard) are
      the same.

  Returns:
    Function with smap applied.
  """
  docstr = (
      "Mapped version of {fun}. Takes similar arguments to {fun} "
      "but with additional array axes over which {fun} is mapped."
  )
  if new_out_axes:
    raise NotImplementedError("New output axes not yet implemented.")

  if shard_size is None:
    return fun

  @_set_docstring(docstr)
  @functools.wraps(fun)
  def mapped_fn(*args, **kwargs):
    # Expand in axes and determine loop range.
    in_axes_ = _expand_axes(in_axes, args)

    in_sizes = jax.tree.map(_maybe_get_size, args, in_axes_)
    in_size = max(jax.tree_util.tree_leaves(in_sizes))

    num_extra_shards = (in_size - 1) // shard_size

    # Fix if necessary.
    last_shard_size = in_size % shard_size
    last_shard_size = shard_size if last_shard_size == 0 else last_shard_size

    def apply_fun_to_slice(slice_start, slice_size):
      input_slice = jax.tree.map(
          lambda array, axis: _maybe_slice(
              array, slice_start, slice_size, axis
          ),
          args,
          in_axes_,
      )
      return fun(*input_slice, **kwargs)

    remainder_shape_dtype = hk.eval_shape(
        partial(apply_fun_to_slice, 0, last_shard_size)
    )
    out_dtypes = jax.tree.map(lambda x: x.dtype, remainder_shape_dtype)
    out_shapes = jax.tree.map(lambda x: x.shape, remainder_shape_dtype)
    out_axes_ = _expand_axes(out_axes, remainder_shape_dtype)

    if num_extra_shards > 0:
      regular_shard_shape_dtype = hk.eval_shape(
          partial(apply_fun_to_slice, 0, shard_size)
      )
      shard_shapes = jax.tree.map(lambda x: x.shape, regular_shard_shape_dtype)

      def make_output_shape(axis, shard_shape, remainder_shape):
        return (
            shard_shape[:axis]
            + (shard_shape[axis] * num_extra_shards + remainder_shape[axis],)
            + shard_shape[axis + 1 :]
        )

      out_shapes = jax.tree.map(
          make_output_shape, out_axes_, shard_shapes, out_shapes
      )

    # Calls dynamic Update slice with different argument order.
    # This is here since tree_map only works with positional arguments.
    def dynamic_update_slice_in_dim(full_array, update, axis, i):
      return jax.lax.dynamic_update_slice_in_dim(full_array, update, i, axis)

    def compute_shard(outputs, slice_start, slice_size):
      slice_out = apply_fun_to_slice(slice_start, slice_size)
      update_slice = partial(dynamic_update_slice_in_dim, i=slice_start)
      return jax.tree.map(update_slice, outputs, slice_out, out_axes_)

    def scan_iteration(outputs, i):
      new_outputs = compute_shard(outputs, i, shard_size)
      return new_outputs, ()

    slice_starts = jnp.arange(0, in_size - shard_size + 1, shard_size)

    def allocate_buffer(dtype, shape):
      return jnp.zeros(shape, dtype=dtype)

    outputs = jax.tree.map(allocate_buffer, out_dtypes, out_shapes)

    if slice_starts.shape[0] > 0:
      outputs, _ = hk.scan(scan_iteration, outputs, slice_starts)

    if last_shard_size != shard_size:
      remainder_start = in_size - last_shard_size
      outputs = compute_shard(outputs, remainder_start, last_shard_size)

    return outputs

  return mapped_fn


def inference_subbatch(
    module: Callable[..., PytreeJaxArray],
    subbatch_size: int,
    batched_args: Sequence[PytreeJaxArray],
    nonbatched_args: Sequence[PytreeJaxArray],
    input_subbatch_dim: int = 0,
    output_subbatch_dim: int | None = None,
) -> PytreeJaxArray:
  """Run through subbatches (like batch apply but with split and concat)."""
  assert len(batched_args) > 0  # pylint: disable=g-explicit-length-test

  if hk.running_init():
    args = list(batched_args) + list(nonbatched_args)
    return module(*args)

  if output_subbatch_dim is None:
    output_subbatch_dim = input_subbatch_dim

  def run_module(*batched_args):
    args = list(batched_args) + list(nonbatched_args)
    res = module(*args)
    return res

  sharded_module = sharded_apply(
      run_module,
      shard_size=subbatch_size,
      in_axes=input_subbatch_dim,
      out_axes=output_subbatch_dim,
  )
  output = sharded_module(*batched_args)

  return output
