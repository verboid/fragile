import copy
from typing import Optional, Tuple

import numpy as np

from fragile.core.base_classes import BaseCritic, BaseWalkers
from fragile.core.states import States
from fragile.core.utils import float_type, relativize, statistics_from_array


class StatesWalkers(States):
    """Keeps track of the data structures used by the `Walkers` class."""

    def __init__(self, batch_size: int, **kwargs):
        """
        Initialize a :class:`StatesWalkers`.

        Args:
            batch_size: Number of walkers that the class will be tracking.
            kwargs: attributes that will not be set as numpy.ndarrays
        """
        self.will_clone = None
        self.compas_ix = None
        self.processed_rewards = None
        self.cum_rewards = None
        self.virtual_rewards = None
        self.distances = None
        self.clone_probs = None
        self.alive_mask = None
        self.id_walkers = None
        self.end_condition = None
        if "state_dict" in kwargs:
            del kwargs["state_dict"]
        super(StatesWalkers, self).__init__(
            state_dict=self.get_params_dict(), batch_size=batch_size, **kwargs
        )

    def get_params_dict(self) -> dict:
        """Return a dictionary containing the param_dict to build an instance \
        of States that can handle all the data generated by the environment.
        """
        params = {
            "id_walkers": {"dtype": np.int64},
            "compas_ix": {"dtype": np.int64},
            "processed_rewards": {"dtype": float_type},
            "virtual_rewards": {"dtype": float_type},
            "cum_rewards": {"dtype": float_type},
            "distances": {"dtype": float_type},
            "clone_probs": {"dtype": float_type},
            "will_clone": {"dtype": np.bool_},
            "alive_mask": {"dtype": np.bool_},
            "end_condition": {"dtype": np.bool_},
        }
        return params

    def clone(self, **kwargs) -> Tuple[np.ndarray, np.ndarray]:
        """Perform the clone only on cum_rewards and id_walkers and reset the other arrays."""
        clone, compas = self.will_clone, self.compas_ix
        self.cum_rewards[clone] = copy.deepcopy(self.cum_rewards[compas][clone])
        self.id_walkers[clone] = copy.deepcopy(self.id_walkers[compas][clone])
        return clone, compas

    def reset(self):
        """Clear the internal data of the class."""
        other_attrs = [name for name in self.keys() if name not in self.get_params_dict()]
        for attr in other_attrs:
            setattr(self, attr, None)
        self.update(
            id_walkers=np.zeros(self.n, dtype=np.int64),
            compas_ix=np.arange(self.n),
            processed_rewards=np.zeros(self.n, dtype=float_type),
            cum_rewards=np.zeros(self.n, dtype=float_type),
            virtual_rewards=np.ones(self.n, dtype=float_type),
            distances=np.zeros(self.n, dtype=float_type),
            clone_probs=np.zeros(self.n, dtype=float_type),
            will_clone=np.zeros(self.n, dtype=np.bool_),
            alive_mask=np.ones(self.n, dtype=np.bool_),
            end_condition=np.zeros(self.n, dtype=np.bool_),
        )


class SimpleWalkers(BaseWalkers):
    """
    This class is in charge of performing all the mathematical operations involved in evolving a \
    cloud of walkers.

    """

    STATE_CLASS = StatesWalkers

    def __init__(
        self,
        n_walkers: int,
        env_state_params: dict,
        model_state_params: dict,
        reward_scale: float = 1.0,
        dist_scale: float = 1.0,
        max_iters: int = 1000,
        accumulate_rewards: bool = True,
        **kwargs
    ):
        """
        Initialize a new `Walkers` instance.

        Args:
            n_walkers: Number of walkers of the instance.
            env_state_params: Dictionary to instantiate the States of an Environment.
            model_state_params: Dictionary to instantiate the States of an Model.
            reward_scale: Regulates the importance of the reward. Recommended to \
                          keep in the [0, 5] range. Higher values correspond to \
                          higher importance.
            dist_scale: Regulates the importance of the distance. Recommended to \
                          keep in the [0, 5] range. Higher values correspond to \
                          higher importance.
            max_iters: Maximum number of iterations that the walkers are allowed \
                       to perform.
            accumulate_rewards: If True the rewards obtained after transitioning \
                                to a new state will accumulate. If False only the last \
                                reward will be taken into account.

        """
        super(SimpleWalkers, self).__init__(
            n_walkers=n_walkers,
            env_state_params=env_state_params,
            model_state_params=model_state_params,
            accumulate_rewards=accumulate_rewards,
        )

        self._model_states: States = States(state_dict=model_state_params, batch_size=n_walkers)
        self._env_states: States = States(state_dict=env_state_params, batch_size=n_walkers)
        self._states = self.STATE_CLASS(batch_size=n_walkers, **kwargs)
        self.reward_scale = reward_scale
        self.dist_scale = dist_scale
        self.n_iters = 0
        self.max_iters = max_iters

    def __len__(self) -> int:
        return self.n

    def __repr__(self) -> str:
        """Print all the data involved in the current run of the algorithm."""
        try:
            text = self._print_stats()
            text += "Walkers States: {}\n".format(self._repr_state(self._states))
            text += "Env States: {}\n".format(self._repr_state(self._env_states))
            text += "Model States: {}\n".format(self._repr_state(self._model_states))
            return text
        except Exception as e:
            return super(SimpleWalkers, self).__repr__()

    def _print_stats(self) -> str:
        """Print several statistics of the current state of the swarm."""
        text = (
            "{} iteration {} Best reward: {:.2f} Dead walkers: {:.2f}% Cloned: {:.2f}%\n\n"
        ).format(
            self.__class__.__name__,
            self.n_iters,
            self.states.cum_rewards.max(),
            100 * self.states.end_condition.sum() / self.n,
            100 * self.states.will_clone.sum() / self.n,
        )
        return text

    @property
    def states(self) -> StatesWalkers:
        """Return the `StatesWalkers` class that contains the data used by the instance."""
        return self._states

    @property
    def env_states(self) -> States:
        """Return the `States` class that contains the data used by an environment."""
        return self._env_states

    @property
    def model_states(self) -> States:
        """Return the `States` class that contains the data used by a Model."""
        return self._model_states

    def calculate_end_condition(self) -> bool:
        """
        Process data from the current state to decide if the iteration process should stop.

        Returns:
            Boolean indicating if the iteration process should be finished. True means \
            it should be stopped, and False means it should continue.

        """
        all_dead = self.states.end_condition.sum() == self.n
        max_iters = self.n_iters > self.max_iters
        self.n_iters += 1
        return all_dead or max_iters

    def calculate_distances(self):
        """Calculate the corresponding distance function for each state with \
        respect to another state chosen at random.

        The internal state is update with the relativized distance values.
        """
        self.states.compas_ix = np.random.permutation(np.arange(self.n))  # self.get_alive_compas()
        obs = self.env_states.observs.reshape(self.n, -1)
        distances = np.linalg.norm(obs - obs[self.states.compas_ix], axis=1)
        distances = relativize(distances.flatten())
        self.update_states(distances=distances)

    def calculate_virtual_reward(self):
        """
        Calculate the virtual reward and update the internal state.

        The cumulative_reward is transformed with the relativize function. \
        The distances stored in the internal state are already assumed to be transformed.
        """
        processed_rewards = relativize(self.states.cum_rewards)
        virt_rw = processed_rewards ** self.reward_scale * self.states.distances ** self.dist_scale
        self.update_states(virtual_rewards=virt_rw, processed_rewards=processed_rewards)

    def get_alive_compas(self) -> np.ndarray:
        """
        Return the indexes of alive companions chosen at random.

        Returns:
            Numpy array containing the int indexes of alive walkers chosen at random with
            repetition.

        """
        self.states.alive_mask = np.logical_not(self.states.end_condition)
        if not self.states.alive_mask.any():  # No need to sample if all walkers are dead.
            return np.arange(self.n)
        compas_ix = np.arange(self.n)[self.states.alive_mask]
        compas = self.random_state.choice(compas_ix, self.n, replace=True)
        compas[: len(compas_ix)] = compas_ix
        return compas

    def update_clone_probs(self):
        """
        Calculate the new probability of cloning for each walker.

        Updates the internal state with both the probability of cloning and the index of the
        randomly chosen companions that were selected to compare the virtual rewards.
        """
        all_virtual_rewards_are_equal = (
            self.states.virtual_rewards == self.states.virtual_rewards[0]
        ).all()
        if all_virtual_rewards_are_equal:
            clone_probs = np.zeros(self.n, dtype=float_type) / float(self.n)
            compas_ix = np.arange(self.n)
        else:
            compas_ix = self.get_alive_compas()
            # This value can be negative!!
            companions = self.states.virtual_rewards[compas_ix]
            clone_probs = (companions - self.states.virtual_rewards) / self.states.virtual_rewards
        self.update_states(clone_probs=clone_probs, compas_ix=compas_ix)

    # @profile
    def balance(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Perform an iteration of the FractalAI algorithm for balancing distributions.

        It performs the necessary calculations to determine which walkers will clone, \
        and performs the cloning process.

        Returns:
            A tuple containing two sets: The first one represent the unique ids \
            of the states for each walker at the start of the iteration. The second \
            one contains the ids of the states after the cloning process.

        """
        old_ids = self.states.id_walkers.astype(int)
        self.calculate_distances()
        self.calculate_virtual_reward()
        self.update_clone_probs()
        self.clone_walkers()
        new_ids = self.states.id_walkers.astype(int)
        return old_ids, new_ids

    def clone_walkers(self):
        """Sample the clone probability distribution and clone the walkers accordingly."""
        will_clone = self.states.clone_probs > self.random_state.random_sample(self.n)
        # Dead walkers always clone
        dead_ix = np.arange(self.n)[self.states.end_condition]
        will_clone[dead_ix] = 1
        self.update_states(will_clone=will_clone)

        clone, compas = self.states.clone()
        self._env_states.clone(will_clone=clone, compas_ix=compas)
        self._model_states.clone(will_clone=clone, compas_ix=compas)

    def reset(self, env_states: States = None, model_states: States = None):
        """
        Restart all the internal states involved in the algorithm iteration.

        After reset a new run of the algorithm will be ready to be launched.
        """
        self.states.reset()
        self.update_states(env_states=env_states, model_states=model_states)
        self.n_iters = 0

    def update_states(self, env_states: States = None, model_states: States = None, **kwargs):
        """
        Update the States variables that do not contain internal data and \
        accumulate the rewards in the internal states if applicable.

        Args:
            env_states: States containing the data associated with the Environment.
            model_states: States containing data associated with the Environment.
            **kwargs: Internal states will be updated via keyword arguments.

        """
        if kwargs:
            if kwargs.get("rewards") is not None:
                self._accumulate_and_update_rewards(kwargs["rewards"])
                del kwargs["rewards"]
            self.states.update(**kwargs)
        if isinstance(env_states, States):
            self._env_states.update(env_states)
            if hasattr(env_states, "rewards"):
                self._accumulate_and_update_rewards(env_states.rewards)
        if isinstance(model_states, States):
            self._model_states.update(model_states)

    def _accumulate_and_update_rewards(self, rewards: np.ndarray):
        """
        Use as reward either the sum of all the rewards received during the \
        current run, or use the last reward value received as reward.

        Args:
            rewards: Array containing the last rewards received by every walker.
        """
        if self._accumulate_rewards:
            if not isinstance(self.states.get("cum_rewards"), np.ndarray):
                cum_rewards = np.zeros(self.n)
            else:
                cum_rewards = self.states.cum_rewards
            cum_rewards = cum_rewards + rewards
        else:
            cum_rewards = rewards
        self.update_states(cum_rewards=cum_rewards)

    @staticmethod
    def _repr_state(state):
        string = "\n"
        for k, v in state.items():
            if k in ["observs", "states"]:
                continue
            shape = v.shape if hasattr(v, "shape") else None
            new_str = "{} shape {} Mean: {:.3f}, Std: {:.3f}, Max: {:.3f} Min: {:.3f}\n".format(
                k, shape, *statistics_from_array(v)
            )
            string += new_str
        return string

    def fix_best(self):
        pass


class Walkers(SimpleWalkers):
    def __init__(self, critic: BaseCritic = None, minimize: bool = False,
                 best_reward_found: float = -1e10, best_found: Optional[np.ndarray] = None,
                 *args, **kwargs):
        """
        Initialize a :class:`MapperWalkers`.

        Args:
            encoder: Encoder that will be used to calculate the pests.
            *args:
            **kwargs:
        """
        # Add data specific to the child class in the StatesWalkers class as new attributes.
        kwargs["critic_score"] = kwargs.get("critic_score", np.zeros(kwargs["n_walkers"]))
        self.dtype = float_type
        super(Walkers, self).__init__(
            best_reward_found=best_reward_found, best_found=best_found, *args, **kwargs
        )
        self.critic = critic
        self.minimize = minimize

    def __repr__(self):
        text = "\nBest reward found: {:.4f} , Critic: {}\n".format(
            float(self.states.best_reward_found), self.critic
        )
        return text + super(Walkers, self).__repr__()

    def calculate_virtual_reward(self):
        rewards = -1 * self.states.cum_rewards if self.minimize else self.states.cum_rewards
        processed_rewards = relativize(rewards)
        virt_rw = processed_rewards ** self.reward_scale * self.states.distances ** self.dist_scale
        self.update_states(virtual_rewards=virt_rw, processed_rewards=processed_rewards)
        if self.critic is not None:
            self.critic.calculate_pest(
                walkers_states=self.states,
                model_states=self.model_states,
                env_states=self.env_states,
            )
            virt_rew = self.states.virtual_rewards * self.states.critic_score
        else:
            virt_rew = self.states.virtual_rewards
        self.states.update(virtual_rewards=virt_rew)

    def balance(self):
        self.update_best()
        returned = super(Walkers, self).balance()
        if self.critic is not None:
            self.critic.update(
                walkers_states=self.states,
                model_states=self.model_states,
                env_states=self.env_states,
            )
        return returned

    def update_best(self):
        rewards = self.states.cum_rewards
        ix = rewards.argmin() if self.minimize else rewards.argmax()
        best = self.env_states.observs[ix].copy()
        best_reward = float(self.states.cum_rewards[ix])
        best_is_alive = not bool(self.env_states.ends[ix])
        has_improved = (self.states.best_reward_found > best_reward if self.minimize else
                        self.states.best_reward_found < best_reward)
        if has_improved and best_is_alive:
            self.states.update(best_reward_found=best_reward)
            self.states.update(best_found=best)

    def fix_best(self):
        self.env_states.observs[-1] = self.states.best_found
        self.env_states.rewards[-1] = self.states.best_reward_found

    def reset(self, env_states: States = None, model_states: States = None):
        super(Walkers, self).reset(env_states=env_states, model_states=model_states)
        rewards = self.env_states.rewards
        ix = rewards.argmin() if self.minimize else rewards.argmax()
        self.states.update(best_found=copy.deepcopy(self.env_states.observs[ix]))
        self.states.update(best_reward_found=np.inf if self.minimize else -np.inf)
