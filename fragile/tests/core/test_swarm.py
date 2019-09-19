from plangym import AtariEnvironment, ParallelEnvironment
from plangym.minimal import ClassicControl
import pytest

from fragile.core.env import BaseEnvironment, DiscreteEnv
from fragile.core.models import BaseModel, RandomDiscrete
from fragile.core.swarm import Swarm
from fragile.core.walkers import BaseWalkers, Walkers
from fragile.optimize.benchmarks import Rastrigin
from fragile.optimize.mapper import FunctionMapper
from fragile.optimize.models import RandomNormal


def create_cartpole_swarm():
    swarm = Swarm(
        model=lambda x: RandomDiscrete(x),
        walkers=Walkers,
        env=lambda: DiscreteEnv(ClassicControl()),
        n_walkers=15,
        max_iters=200,
        prune_tree=True,
        reward_scale=2,
    )
    return swarm


def create_atari_swarm():
    env = ParallelEnvironment(
        env_class=AtariEnvironment,
        name="MsPacman-ram-v0",
        clone_seeds=True,
        autoreset=True,
        blocking=False,
    )
    swarm = Swarm(
        model=lambda x: RandomDiscrete(x),
        walkers=Walkers,
        env=lambda: DiscreteEnv(env),
        n_walkers=67,
        max_iters=20,
        prune_tree=True,
        reward_scale=2,
    )
    return swarm


def create_function_swarm():
    env = Rastrigin(shape=(2,), high=5.12, low=5.12)
    swarm = FunctionMapper(
        model=lambda x: RandomNormal(x, high=5.12, low=5.12),
        env=lambda: env,
        n_walkers=5,
        max_iters=5,
        prune_tree=True,
        reward_scale=2,
        minimize=False,
    )
    return swarm


swarm_dict = {
    "cartpole": create_cartpole_swarm,
    "atari": create_atari_swarm,
    "function": create_function_swarm,
}


@pytest.fixture()
def swarm(request):
    return TestSwarm.swarm_dict.get(request.param, create_cartpole_swarm)()


class TestSwarm:
    swarm_dict = {
        "cartpole": create_cartpole_swarm,
        "atari": create_atari_swarm,
        "function": create_function_swarm,
    }
    swarm_names = list(swarm_dict.keys())
    test_scores = list(zip(swarm_names, [149, 750, 10]))

    @pytest.mark.parametrize("swarm", swarm_names, indirect=True)
    def test_init_not_crashes(self, swarm):
        assert swarm is not None

    @pytest.mark.parametrize("swarm", swarm_names, indirect=True)
    def test_env_init(self, swarm):
        assert hasattr(swarm.walkers.states, "will_clone")

    @pytest.mark.parametrize("swarm", swarm_names, indirect=True)
    def test_attributes(self, swarm):
        assert isinstance(swarm.env, BaseEnvironment)
        assert isinstance(swarm.model, BaseModel)
        assert isinstance(swarm.walkers, BaseWalkers)

    @pytest.mark.parametrize("swarm", swarm_names, indirect=True)
    def test_reset_no_params(self, swarm):
        swarm.reset()

    @pytest.mark.parametrize("swarm", swarm_names, indirect=True)
    def test_step_does_not_crashes(self, swarm):
        swarm.reset()
        swarm.step_walkers()

    @pytest.mark.parametrize("swarm", swarm_names, indirect=True)
    def test_run_swarm(self, swarm):
        swarm.reset()
        swarm.walkers.max_iters = 5
        swarm.run_swarm()

    @pytest.mark.parametrize("swarm, target", test_scores, indirect=["swarm"])
    def test_score_gets_higher(self, swarm, target):
        swarm.walkers.seed()
        swarm.reset()
        swarm.walkers.max_iters = 150
        swarm.run_swarm()
        reward = swarm.walkers.states.cum_rewards.max()
        assert reward > target, "Iters: {}, rewards: {}".format(
            swarm.walkers.n_iters, swarm.walkers.states.cum_rewards
        )
