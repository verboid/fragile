from typing import Optional, Union

import judo
from judo import Backend, Bounds, dtype, tensor
from numba import jit

from fragile.core.base_classes import BaseCritic, BaseModel
from fragile.core.env import DiscreteEnv
from fragile.core.states import StatesEnv, StatesModel, StatesWalkers
from fragile.core.typing import StateDict, Tensor


class Model(BaseModel):
    """
    Base class that incorporates a critic for adding arbitrary extra \
    computation steps to any kind of Model.

    It defines, resets, handles parameter checking, raises errors and inserts \
    the calculated actions into its corresponding :class:`SwarmState`.
    """

    def __init__(self, critic: Optional[BaseCritic] = None, env=None):
        """
        Initialize a :class:`Model`.

        Args:
            critic: :class:`Critic` used to calculate an additional value.
            env: Ignored. Used to match callable initialization inside a swarm.

        """
        self.critic: BaseCritic = critic

    def sample(
        self,
        batch_size: int,
        model_states: StatesModel = None,
        env_states: StatesEnv = None,
        walkers_states: StatesWalkers = None,
    ) -> StatesModel:
        """
        Calculate the corresponding data to interact with the Environment and \
        store it in the corresponding model_states.

        Args:
            batch_size: Number of new points to the sampled.
            model_states: SwarmState corresponding to the environment data.
            env_states: SwarmState corresponding to the model data.
            walkers_states: SwarmState corresponding to the walkers data.

        Returns:
            Tuple containing a tensor with the sampled actions and the new model states variable.

        """
        raise NotImplementedError

    def predict(
        self,
        batch_size: int = None,
        model_states: StatesModel = None,
        env_states: StatesEnv = None,
        walkers_states: StatesWalkers = None,
    ) -> StatesModel:
        """
        Return SwarmState containing the data to interact with the environment and \
        a dt attribute containing clipped gaussian samples.

        Args:
            batch_size: Number of new points to the sampled. If None, env_states.n \
                        will be used to determine the batch_size.
            model_states: SwarmState corresponding to the environment data.
            env_states: SwarmState corresponding to the model data. Required if \
                        batch_size is None.
            walkers_states: SwarmState corresponding to the walkers data.

        Returns:
            :class:`SwarmState` variable containing the calculated actions.

        """
        if batch_size is None and env_states is None:
            raise ValueError("env_states and batch_size cannot be both None.")
        batch_size = batch_size or env_states.n
        model_states = model_states or self.create_new_states(batch_size=batch_size)
        model_states = self.sample(
            batch_size=batch_size,
            model_states=model_states,
            env_states=env_states,
            walkers_states=walkers_states,
        )
        return model_states

    def reset(
        self,
        batch_size: int = 1,
        model_states: StatesModel = None,
        *args,
        **kwargs,
    ) -> StatesModel:
        """
        Return a new blank State for a `DiscreteUniform` instance, and a valid \
        prediction based on that new state.

        Args:
            batch_size: Number of walkers that the new model `State`.
            model_states: SwarmState corresponding to the environment data.
            *args: Passed to `predict`.
            **kwargs: Passed to `predict`.

        Returns:
            New model states containing sampled data.

        """
        model_states = self.predict(
            batch_size=batch_size,
            model_states=model_states,
            *args,
            **kwargs,
        )
        return model_states

    def add_critic_params(self, params: dict, override_params: bool = True) -> StateDict:
        """
        Update the model parameters dictionary with the :class:`Critic` parameters.

        Args:
            params: Dictionary containing the parameters of the current :class:`Model`.
            override_params: The :class:`Critic` parameters will override the \
            :class:`Model` parameters if they both have parameters with the same name.

        Returns:
            dict containing the parameters of both the :class:`Model` and its :class:`Critic`.

        """
        if self.critic is not None:
            critic_vals = self.critic.get_params_dict()
            if override_params:
                params.update(critic_vals)
            else:
                critic_vals.update(params)
        else:
            critic_vals = params
        return critic_vals

    def update_states_with_critic(
        self,
        actions: Tensor,
        batch_size: int,
        model_states: StatesModel,
        **kwargs,
    ) -> StatesModel:
        """
        Compute the time steps generated by the critic and add them to \
        `model_states`. If there is no Critic the default value of dt will be a \
        vector of 1.

        Args:
            actions: Numpy array representing the actions calculated by the model.
            batch_size: Same batch size used when calling `sample`.
            model_states: Same model_states used when calling `sample`.
            **kwargs: Kwargs for `critic.calculate`.

        Returns:
            model_states updated with the actions and the dt calculated by the Critic.

        """
        if self.critic is None:
            model_states.update(actions=actions)
        else:
            critic_state = self.critic.calculate(
                batch_size=batch_size,
                model_states=model_states,
                **kwargs,
            )
            model_states.update(other=critic_state, actions=actions)
        return model_states


class _DtModel(Model):
    """
    Model class that allows to sample actions meant to be applied a different \
    number of time steps. In order to account for the target number of time \
    steps it incorporates in the `dt` attribute, that will represent the number of \
    times that the calculated action should be applied.

    This model is not meant to be instantiated directly but used for class inheritance.
    """

    def get_params_dict(self, override_params: bool = True) -> StateDict:
        """
        Return the dictionary with the parameters to create a new `DiscreteUniform` model.

        Args:
            override_params: The :class:`Critic` parameters will override the \
            :class:`Model` parameters if they both have parameters with the same name.

        Returns:
            dict containing the parameters of both the :class:`Model` and its :class:`Critic`.

        """
        dt = {"dt": {"dtype": dtype.float32}, "critic_score": {"dtype": dtype.int64}}
        all_params = self.add_critic_params(params=dt, override_params=override_params)
        return all_params

    def update_states_with_critic(
        self,
        actions: Tensor,
        batch_size: int,
        model_states: StatesModel,
        critic_kwargs=None,
        **kwargs,
    ) -> StatesModel:
        """
        Compute the time steps generated by the critic and add them to \
        `model_states`. If there is no Critic the default value of dt will be a \
        vector of 1.

        Args:
            actions: Numpy array representing the actions calculated by the model.
            batch_size: Same batch size used when calling `sample`.
            model_states: Same model_states used when calling `sample`.
            critic_kwargs: Passed to critic.calculate().
            **kwargs: Passed to model_states.update().

        Returns:
            model_states updated with the actions and the dt calculated by the Critic.

        """
        if self.critic is not None:
            critic_kwargs = critic_kwargs if critic_kwargs is not None else dict()
            critic_states = self.critic.calculate(
                batch_size=batch_size,
                model_states=model_states,
                **critic_kwargs,
            )

            dt = (
                critic_states.critic_score
                if judo.is_tensor(critic_states.critic_score)
                else critic_states.critic_score
            )
            model_states.update(actions=actions, other=critic_states, dt=dt)
        else:
            dt = judo.ones(batch_size, dtype=dtype.int64)
            model_states.update(actions=actions, critic_score=dt, dt=dt, **kwargs)
        return model_states


class DiscreteModel(_DtModel):
    """It represents the base Model class that handles a discrete set of possible outcomes."""

    def __init__(
        self,
        n_actions: int = None,
        critic: BaseCritic = None,
        env: DiscreteEnv = None,
    ):
        """
        Initialize a :class:`DiscreteModel`.

        Args:
            n_actions: Number of different discrete outcomes that the model can provide.
            env: :class:`DiscreteEnvironment` that will be used to extract the \
            number of different possible outcomes.
            critic: Critic used to calculate the time step strategy.

        """
        super(DiscreteModel, self).__init__(critic=critic)
        if n_actions is None and env is None:
            raise ValueError("Env and n_actions cannot be both None.")
        self._n_actions = env.n_actions if n_actions is None else n_actions

    @property
    def n_actions(self) -> int:
        """Return the number of different possible discrete actions that the model can output."""
        return self._n_actions

    def get_params_dict(self, override_params: bool = True) -> StateDict:
        """Return the dictionary with the parameters to create a new `DiscreteUniform` model."""
        params = super(DiscreteModel, self).get_params_dict(override_params=override_params)
        params.update({"actions": {"dtype": dtype.int64}})
        return params


class DiscreteUniform(DiscreteModel):
    """
    Model that samples actions in a one dimensional discrete state space using \
    a uniform prior. For each walker on the batch it will return an integer in \
    the interval [0, n_actions].
    """

    def sample(self, batch_size: int, model_states: StatesModel = None, **kwargs) -> StatesModel:
        """
        Sample a random discrete variable from a uniform prior.

        Args:
            batch_size: Number of new points to the sampled.
            model_states: SwarmState corresponding to the environment data.
            kwargs: passed to the :class:`Critic`.

        Returns:
            :class:`SwarmState` variable containing the calculated actions and dt.

        """
        actions = self.random_state.randint(0, self.n_actions, size=batch_size)
        return self.update_states_with_critic(
            actions=actions,
            model_states=model_states,
            batch_size=batch_size,
            **kwargs,
        )


class BinarySwap(DiscreteModel):
    """
    This model acts on a vector of binary values and swaps the values of a \
    given number of dimensions chosen at random.
    """

    def __init__(
        self,
        n_swaps: Optional[int] = 1,
        n_actions: int = None,
        critic: BaseCritic = None,
        env: DiscreteEnv = None,
    ):
        """
        Initialize a :class:`BinarySwap`.

        Args:
            n_swaps: Number of binary dimensions that will be swapped every time \
                     `sample` is called. If `n_swaps` is None it will be the same \
                     as n_actions.
            n_actions: Number of different discrete outcomes that the model can provide.
            env: :class:`DiscreteEnvironment` that will be used to extract the \
            dimension of the target vector.
            critic: dt_sampler used to calculate an additional time step strategy.

        """
        super(BinarySwap, self).__init__(critic=critic, n_actions=n_actions, env=env)
        if n_swaps <= 0:
            raise ValueError("n_swaps must be greater than 0.")
        self.n_swaps = n_swaps if n_swaps is not None else self.n_actions

    def get_params_dict(self, override_params: bool = True) -> StateDict:
        """Return the dictionary with the parameters to create a new :class:`BinarySwap` model."""
        all_params = super(BinarySwap, self).get_params_dict(override_params=override_params)
        actions = {"actions": {"dtype": dtype.int64, "size": (self.n_actions,)}}
        all_params.update(actions)
        return all_params

    def sample(
        self,
        batch_size: int,
        env_states: StatesEnv = None,
        model_states: StatesModel = None,
        **kwargs,
    ) -> StatesModel:
        """
        Swap the values of `n_swaps` dimensions chosen at random. It works on a \
        matrix of binary values of size (batch_size, n_actions).

        Args:
            batch_size: Number of new points to the sampled.
            model_states: :class:`StatesModel` corresponding to the :class:`Model` data.
            env_states: :class:`StatesEnv` of the algorithm's :class:`Environment`.
            kwargs: Passed to the :class:`Critic`.

        Returns:
            :class:`SwarmState` variable containing the calculated actions and dt.

        """
        import numpy

        @jit(nopython=True)
        def flip_values(actions: numpy.ndarray, flips: numpy.ndarray):
            for i in range(flips.shape[0]):
                for j in range(flips.shape[1]):
                    actions[i, flips[i, j]] = numpy.logical_not(actions[i, flips[i, j]])
            return actions

        with Backend.use_backend("numpy"):
            actions = (
                judo.to_numpy(env_states.observs).copy()
                if env_states is not None
                else judo.zeros((batch_size, self.n_actions))
            )
            actions = judo.astype(actions, dtype.bool)

            flips = self.random_state.randint(0, self.n_actions, size=(batch_size, self.n_swaps))
            actions = judo.astype(flip_values(actions, flips), dtype.int64)
        actions = tensor(actions)
        return self.update_states_with_critic(
            actions=actions,
            batch_size=batch_size,
            model_states=model_states,
            **kwargs,
        )


class ContinuousModel(_DtModel):
    """
    It represents the base Model class that handles a continuous interval of \
    possible outcomes.
    """

    def __init__(
        self,
        bounds: Bounds = None,
        critic: Optional[BaseCritic] = None,
        env=None,
        **kwargs,
    ):
        """
        Initialize a :class:`RandomContinuous`.

        Args:
            bounds: :class:`Bounds` class defining the range of allowed output \
            values of the model.
            critic: :class:`Critic` that will be used to make additional computation.
            env: Environment used to infer the bounds of the model.
            **kwargs: Ignored. Only defined to march :class:`Model` interface.

        """
        try:
            bounds = bounds if bounds is not None else env.bounds
        except Exception:
            raise ValueError("bounds is None and env.bounds is not available.")
        super(ContinuousModel, self).__init__(critic=critic)
        self.bounds = bounds

    @property
    def shape(self) -> tuple:
        """Return the shape of the sampled random variable."""
        return self.bounds.shape

    @property
    def n_dims(self) -> int:
        """Return the number of dimensions of the sampled random variable."""
        return self.bounds.shape[0]

    def get_params_dict(self, override_params: bool = True) -> StateDict:
        """Return the dictionary with the parameters to create a new `DiscreteUniform` model."""
        all_params = super(ContinuousModel, self).get_params_dict(override_params=override_params)
        actions = {"actions": {"size": self.shape, "dtype": dtype.float32}}
        all_params.update(actions)
        return all_params


class ContinuousUniform(ContinuousModel):
    """Model that samples continuous actions in a given interval using a uniform prior."""

    def sample(self, batch_size: int, model_states: StatesModel = None, **kwargs) -> StatesModel:
        """
        Sample a random continuous variable from a uniform prior.

        Args:
            batch_size: Number of new points to the sampled.
            model_states: SwarmState corresponding to the model data.
            kwargs: passed to the :class:`Critic`.

        Returns:
            SwarmState containing the new sampled discrete random values inside \
            `state.actions` attribute.

        """
        actions = self.random_state.uniform(
            low=self.bounds.low,
            high=self.bounds.high,
            size=tuple([batch_size]) + self.shape,
        )
        actions = judo.astype(actions, self.bounds.dtype)
        return self.update_states_with_critic(
            actions=actions,
            batch_size=batch_size,
            model_states=model_states,
            **kwargs,
        )


class NormalContinuous(ContinuousModel):
    """
    Calculate continuous actions inside the given :class:`Bounds` sampling from \
    a normal distribution with the provided mean and standard deviation.
    """

    def __init__(
        self,
        bounds: Bounds = None,
        loc: Union[int, float, Tensor] = 0.0,
        scale: Optional[Union[int, float, Tensor]] = 1.0,
        critic: Optional[BaseCritic] = None,
        **kwargs,
    ):
        """
        Initialize a :class:`RandomContinuous`.

        Args:
            bounds: :class:`Bounds` class defining the range of allowed values for the model.
            loc: Mean of the gaussian distribution used for sampling actions.
            scale: Standard deviation of the gaussian distribution used for sampling actions.
            critic: :class:`Critic` that will be used to make additional computation.
            **kwargs: Ignored. Only defined to march :class:`Model` interface.

        """
        super(NormalContinuous, self).__init__(critic=critic, bounds=bounds, **kwargs)
        self.loc = loc
        self.scale = scale

    def sample(
        self,
        batch_size: int,
        model_states: StatesModel = None,
        env_states: StatesEnv = None,
        walkers_states: "StatesWalkers" = None,
        **kwargs,
    ) -> StatesModel:
        """
        Calculate the actions sampling from a Gaussian distribution.

        Args:
            batch_size: Number of new points to the sampled.
            model_states: SwarmState corresponding to the environment data.
            env_states: SwarmState corresponding to the model data.
            walkers_states: SwarmState corresponding to the walkers data.
            kwargs: passed to the :class:`Critic`.

        Returns:
            :class:`SwarmState` variable containing the calculated actions and dt.

        """
        actions = self.random_state.normal(
            size=tuple([batch_size]) + self.shape,
            loc=self.loc,
            scale=self.scale,
        )
        actions = self.bounds.clip(actions)
        return self.update_states_with_critic(
            actions=actions,
            batch_size=batch_size,
            model_states=model_states,
            **kwargs,
        )