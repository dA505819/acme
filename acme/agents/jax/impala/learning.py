# python3
# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
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

"""Learner for the IMPALA actor-critic agent."""

import functools
from typing import Callable, Dict, Iterator, List, NamedTuple, Tuple

import acme
from acme import specs
from acme.networks import jax as networks
from acme.utils import counting
from acme.utils import jax_utils
from acme.utils import loggers

import haiku as hk
import jax
from jax.experimental import optix
import jax.numpy as jnp
import numpy as np
import reverb
import rlax
import tree


class TrainingState(NamedTuple):
  params: hk.Params
  opt_state: optix.OptState


class IMPALALearner(acme.Learner, acme.Saveable):
  """Learner for an importanced-weighted advantage actor-critic."""

  def __init__(
      self,
      network: networks.PolicyValueRNN,
      initial_state_fn: Callable[[], networks.RNNState],
      obs_spec: specs.Array,
      iterator: Iterator[reverb.ReplaySample],
      optimizer: optix.InitUpdate,
      rng: hk.PRNGSequence,
      discount: float = 0.99,
      entropy_cost: float = 0.,
      baseline_cost: float = 1.,
      max_abs_reward: float = np.inf,
      counter: counting.Counter = None,
      logger: loggers.Logger = None,
  ):

    # Transform into pure functions.
    network = hk.transform(network)
    initial_state_fn = hk.transform(initial_state_fn)

    def loss(params: hk.Params, sample: reverb.ReplaySample) -> jnp.ndarray:
      """Entropy-regularised actor-critic loss."""

      # Extract the data.
      observations, actions, rewards, discounts, extra = sample.data
      initial_state = tree.map_structure(lambda s: s[0], extra['core_state'])
      behaviour_logits = extra['logits']

      #
      actions = actions[:-1]  # [T-1]
      rewards = rewards[:-1]  # [T-1]
      discounts = discounts[:-1]  # [T-1]
      rewards = jnp.clip(rewards, -max_abs_reward, max_abs_reward)

      # Unroll current policy over observations.
      net = functools.partial(network.apply, params)
      (logits, values), _ = hk.static_unroll(net, observations, initial_state)

      # Compute importance sampling weights: current policy / behavior policy.
      rhos = rlax.categorical_importance_sampling_ratios(
          logits[:-1], behaviour_logits[:-1], actions)

      # Critic loss.
      vtrace_returns = rlax.vtrace_td_error_and_advantage(
          v_tm1=values[:-1],
          v_t=values[1:],
          r_t=rewards,
          discount_t=discounts * discount,
          rho_t=rhos)
      critic_loss = jnp.square(vtrace_returns.errors)

      # Policy gradient loss.
      policy_gradient_loss = rlax.policy_gradient_loss(
          logits_t=logits[:-1],
          a_t=actions,
          adv_t=vtrace_returns.pg_advantage,
          w_t=jnp.ones_like(rewards))

      # Entropy regulariser.
      entropy_loss = rlax.entropy_loss(logits[:-1], jnp.ones_like(rewards))

      # Combine weighted sum of actor & critic losses.
      mean_loss = jnp.mean(policy_gradient_loss + baseline_cost * critic_loss +
                           entropy_cost * entropy_loss)

      return mean_loss

    @jax.jit
    def sgd_step(
        state: TrainingState, sample: reverb.ReplaySample
    ) -> Tuple[TrainingState, Dict[str, jnp.ndarray]]:
      """Computes an SGD step, returning new state and metrics for logging."""

      # Compute gradients.
      batch_loss = jax.vmap(loss, in_axes=(None, 0))
      mean_loss = lambda p, s: jnp.mean(batch_loss(p, s))
      grad_fn = jax.value_and_grad(mean_loss)
      loss_value, gradients = grad_fn(state.params, sample)

      # Apply updates
      updates, new_opt_state = optimizer.update(gradients, state.opt_state)
      new_params = optix.apply_updates(state.params, updates)

      metrics = {
          'loss': loss_value,
      }

      new_state = TrainingState(params=new_params, opt_state=new_opt_state)

      return new_state, metrics

    def make_initial_state(key: jnp.ndarray) -> TrainingState:
      """Initialises the training state (parameters and optimiser state)."""
      dummy_obs = jax_utils.zeros_like(obs_spec)
      initial_state = initial_state_fn.apply(None)
      initial_params = network.init(key, dummy_obs, initial_state)
      initial_opt_state = optimizer.init(initial_params)
      return TrainingState(params=initial_params, opt_state=initial_opt_state)

    self._state = make_initial_state(next(rng))

    # Internalise iterator.
    self._iterator = jax_utils.prefetch(iterator)
    self._sgd_step = sgd_step

    # Set up logging/counting.
    self._counter = counter or counting.Counter()
    self._logger = logger or loggers.TerminalLogger('learner', time_delta=1.)

  def step(self):
    """Does a step of SGD and logs the results."""

    sample = next(self._iterator)
    self._state, results = self._sgd_step(self._state, sample)

    # Update our counts and record it.
    counts = self._counter.increment(steps=1)
    results = {k: np.array(v) for k, v in results.items()}
    results.update(counts)

    # Snapshot and attempt to write logs.
    self._logger.write(results)

  def get_variables(self, names: List[str]) -> List[hk.Params]:
    return [self._state.params]

  def save(self) -> TrainingState:
    return self._state

  def restore(self, state: TrainingState):
    self._state = state
