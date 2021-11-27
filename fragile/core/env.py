from typing import Dict, Union

import judo
from judo import Backend, tensor

from fragile.core.base_classes import BaseEnvironment
from fragile.core.states import StatesEnv, StatesModel
from fragile.core.typing import StateDict, Tensor


class Environment(BaseEnvironment):
    """
    The Environment is in charge of stepping the walkers, acting as an state \
    transition function.

    For every different problem a new :class:`Environment` needs to be implemented \
    following the :class:`BaseEnvironment` interface.
    """

    def __init__(
        self,
        states_shape: tuple,
        observs_shape: tuple,
        states_dtype: type = judo.float64,
    ):
        """
        Initialize an :class:`Environment`.

        Args:
            states_shape: Shape of the internal state of the :class:`Environment`.
            observs_shape: Shape of the observations state of the :class:`Environment`.
            states_dtype: Type of the internal state of the :class:`Environment`.

        """
        self._states_shape = states_shape
        self._observs_shape = observs_shape
        self._states_dtype = states_dtype

    @property
    def states_shape(self) -> tuple:
        """Return the shape of the internal state of the :class:`Environment`."""
        return self._states_shape

    @property
    def states_dtype(self) -> type:
        """Return the shape of the internal state of the :class:`Environment`."""
        return self._states_dtype

    @property
    def observs_shape(self) -> tuple:
        """Return the shape of the observations state of the :class:`Environment`."""
        return self._observs_shape

    def get_params_dict(self) -> StateDict:
        """
        Return a dictionary containing the param_dict to build an instance \
        of :class:`StatesEnv` that can handle all the data generated by an \
        :class:`Environment`.
        """
        params = {
            "states": {"size": self.states_shape, "dtype": self._states_dtype},
            "observs": {"size": self.observs_shape, "dtype": judo.float32},
            "rewards": {"dtype": judo.float32},
            "times": {"dtype": judo.float32},
            "oobs": {"dtype": judo.bool},
            "terminals": {"dtype": judo.bool},
        }
        return params

    def states_from_data(
        self,
        batch_size: int,
        states,
        observs,
        rewards,
        oobs,
        terminals=None,
        **kwargs,
    ) -> StatesEnv:
        """Return a new :class:`StatesEnv` object containing the data generated \
        by the environment."""
        oobs = tensor(oobs, dtype=judo.bool)
        terminals = (
            tensor(oobs, dtype=judo.bool)
            if terminals is not None
            else judo.zeros(len(oobs), dtype=judo.bool)
        )
        rewards = tensor(rewards, dtype=judo.float32)
        observs = tensor(observs)
        try:
            states = tensor(states)
        except Exception as e:
            print(Backend.get_current_backend())
            raise e
        state = super(Environment, self).states_from_data(
            batch_size=batch_size,
            states=states,
            observs=observs,
            rewards=rewards,
            oobs=oobs,
            terminals=terminals,
            **kwargs,
        )
        return state


class DiscreteEnv(Environment):
    """The DiscreteEnv acts as an interface with `plangym` discrete actions.

    It can interact with any environment that accepts discrete actions and \
    follows the interface of `plangym`.
    """

    # fmt: off
    def __init__(self, env: "plangym.core.GymEnvironment", states_dtype: type = judo.float64):  # noqa: F821 E501 fmt: on
        """
        Initialize a :class:`DiscreteEnv`.

        Args:
           env: Instance of :class:`plangym.Environment`.
           states_dtype: Type of the internal state of the :class:`Environment`.

        """
        self._env = env
        self._n_actions = (
            self._env.action_space.n
            if hasattr(self._env.action_space, "n")
            else self._env.action_space.shape[0]
        )
        super(DiscreteEnv, self).__init__(
            states_shape=self._env.get_state().shape,
            observs_shape=self._env.observation_space.shape,
            states_dtype=states_dtype,
        )

    @property
    def action_space(self):
        """Return the ``action_space`` of the wrapped :class:`plangym.GymEnvironment`."""
        return self._env.action_space

    @property
    def observation_space(self):
        """Return the ``action_space`` of the wrapped :class:`plangym.GymEnvironment`."""
        return self._env.observation_space

    @property
    def n_actions(self) -> int:
        """Return the number of different discrete actions that can be taken in the environment."""
        return self._n_actions

    def states_to_data(
        self, model_states: StatesModel, env_states: StatesEnv,
    ) -> Dict[str, Tensor]:
        """
        Extract the data that will be used to make the state transitions.

        Args:
            model_states: :class:`StatesModel` representing the data to be used \
                         to act on the environment.
            env_states: :class:`StatesEnv` representing the data to be set in \
                       the environment.

        Returns:
            Dictionary containing:

            ``{"states": np.array, "actions": np.array, "dt": np.array/int}``

        """
        actions = judo.astype(model_states.actions, judo.int32)
        dt = model_states.dt if hasattr(model_states, "dt") else 1
        data = {"states": env_states.states, "actions": actions, "dt": dt}
        return data

    def make_transitions(
        self, states: Tensor, actions: Tensor, dt: Union[Tensor, int],
    ) -> Dict[str, Tensor]:
        """
        Step the underlying :class:`plangym.Environment` using the ``step_batch`` \
        method of the ``plangym`` interface.
        """
        dt = judo.to_numpy(dt) if judo.is_tensor(dt) else dt
        new_states, observs, rewards, ends, infos = self._env.step_batch(
            actions=judo.to_numpy(actions), states=judo.to_numpy(states), dt=dt,
        )
        game_ends = [inf.get("win", False) for inf in infos]
        data = {
            "states": tensor(new_states),
            "observs": tensor(observs),
            "rewards": tensor(rewards),
            "oobs": tensor(ends),
            "terminals": tensor(game_ends),
        }
        return data

    def reset(self, batch_size: int = 1, **kwargs) -> StatesEnv:
        """
        Reset the environment to the start of a new episode and return a new \
        :class:`StatesEnv` instance describing the state of the :env:`Environment`.

        Args:
            batch_size: Number of walkers that the returned state will have.
            **kwargs: Ignored. This environment resets without using any external data.

        Returns:
            States instance describing the state of the Environment. The first \
            dimension of the data tensors (number of walkers) will be equal to \
            batch_size.

        """
        with Backend.use_backend("numpy"):
            state, obs = self._env.reset()
            states = tensor([state.copy() for _ in range(batch_size)]).copy()
            observs = tensor([obs.copy() for _ in range(batch_size)]).copy().astype(judo.float32)
        # observs = tensor(observs)
        # states = tensor(states)
        rewards = judo.zeros(batch_size, dtype=judo.float32)
        times = judo.zeros_like(rewards)
        oobs = judo.zeros(batch_size, dtype=judo.bool)
        new_states = self.states_from_data(
            batch_size=batch_size,
            states=states,
            observs=observs,
            rewards=rewards,
            oobs=oobs,
            times=times,
        )
        return new_states
