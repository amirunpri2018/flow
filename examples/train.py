"""Runner script for single and multi-agent reinforcement learning experiments.

This script performs an RL experiment using the PPO algorithm. Choice of
hyperparameters can be seen and adjusted from the code below.

Usage
    python train.py EXP_CONFIG
"""
import argparse
from datetime import datetime
import json
import os
import sys
from time import strftime
from copy import deepcopy
import numpy as np
import pytz

from flow.core.util import ensure_dir
from flow.core.rewards import instantaneous_mpg
from flow.utils.registry import env_constructor
from flow.utils.rllib import FlowParamsEncoder, get_flow_params
from flow.utils.registry import make_create_env
from flow.replay.transfer_tests import create_parser, generate_graphs


def parse_args(args):
    """Parse training options user can specify in command line.

    Returns
    -------
    argparse.Namespace
        the output parser object
    """
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Parse argument used when running a Flow simulation.",
        epilog="python train.py EXP_CONFIG")

    # required input parameters
    parser.add_argument(
        'exp_config', type=str,
        help='Name of the experiment configuration file, as located in '
             'exp_configs/rl/singleagent or exp_configs/rl/multiagent.')

    # optional input parameters
    parser.add_argument(
        '--rl_trainer', type=str, default="rllib",
        help='the RL trainer to use. either rllib or Stable-Baselines')
    parser.add_argument(
        '--algorithm', type=str, default="PPO",
        help='RL algorithm to use. Options are PPO, TD3, and CENTRALIZEDPPO (which uses a centralized value function)'
             ' right now.'
    )
    parser.add_argument(
        '--num_cpus', type=int, default=1,
        help='How many CPUs to use')
    parser.add_argument(
        '--num_steps', type=int, default=5000,
        help='How many total steps to perform learning over. Relevant for stable-baselines')
    parser.add_argument(
        '--grid_search', action='store_true', default=False,
        help='Whether to grid search over hyperparams')
    parser.add_argument(
        '--num_iterations', type=int, default=200,
        help='How many iterations are in a training run.')
    parser.add_argument(
        '--checkpoint_freq', type=int, default=20,
        help='How often to checkpoint.')
    parser.add_argument(
        '--num_rollouts', type=int, default=1,
        help='How many rollouts are in a training batch')
    parser.add_argument(
        '--rollout_size', type=int, default=1000,
        help='How many steps are in a training batch.')
    parser.add_argument('--use_s3', action='store_true', default=False,
                        help='If true, upload results to s3')
    parser.add_argument('--local_mode', action='store_true', default=False,
                        help='If true only 1 CPU will be used')
    parser.add_argument('--render', action='store_true', default=False,
                        help='If true, we render the display')
    parser.add_argument(
        '--checkpoint_path', type=str, default=None,
        help='Directory with checkpoint to restore training from.')
    parser.add_argument(
        '--exp_title', type=str, default=None,
        help='Name of experiment that results will be stored in')
    parser.add_argument('--multi_node', action='store_true',
                        help='Set to true if this will be run in cluster mode.'
                             'Relevant for rllib')
    parser.add_argument(
        '--upload_graphs', type=str, nargs=2,
        help='Whether to generate and upload graphs to leaderboard at the end of training.'
             'Arguments are name of the submitter and name of the strategy.'
             'Only relevant for i210 training on rllib')

    return parser.parse_known_args(args)[0]


def run_model_stablebaseline(flow_params,
                             num_cpus=1,
                             rollout_size=50,
                             num_steps=50):
    """Run the model for num_steps if provided.

    Parameters
    ----------
    flow_params : dict
        flow-specific parameters
    num_cpus : int
        number of CPUs used during training
    rollout_size : int
        length of a single rollout
    num_steps : int
        total number of training steps
    The total rollout length is rollout_size.

    Returns
    -------
    stable_baselines.*
        the trained model
    """
    from stable_baselines.common.vec_env import DummyVecEnv, SubprocVecEnv
    from stable_baselines import PPO2
    if num_cpus == 1:
        constructor = env_constructor(params=flow_params, version=0)()
        # The algorithms require a vectorized environment to run
        env = DummyVecEnv([lambda: constructor])
    else:
        env = SubprocVecEnv([env_constructor(params=flow_params, version=i)
                             for i in range(num_cpus)])

    train_model = PPO2('MlpPolicy', env, verbose=1, n_steps=rollout_size)
    train_model.learn(total_timesteps=num_steps)
    return train_model


def setup_exps_rllib(flow_params,
                     n_cpus,
                     n_rollouts,
                     flags,
                     policy_graphs=None,
                     policy_mapping_fn=None,
                     policies_to_train=None,
                     ):
    """Return the relevant components of an RLlib experiment.

    Parameters
    ----------
    flow_params : dict
        flow-specific parameters (see flow/utils/registry.py)
    n_cpus : int
        number of CPUs to run the experiment over
    n_rollouts : int
        number of rollouts per training iteration
    flags : TODO
        custom arguments
    policy_graphs : dict, optional
        TODO
    policy_mapping_fn : function, optional
        TODO
    policies_to_train : list of str, optional
        TODO
    Returns
    -------
    str
        name of the training algorithm
    str
        name of the gym environment to be trained
    dict
        training configuration parameters
    """
    from ray import tune
    from ray.tune.registry import register_env
    from ray.rllib.env.group_agents_wrapper import _GroupAgentsWrapper
    try:
        from ray.rllib.agents.agent import get_agent_class
    except ImportError:
        from ray.rllib.agents.registry import get_agent_class

    horizon = flow_params['env'].horizon

    alg_run = flags.algorithm.upper()

    if alg_run == "PPO":
        from flow.algorithms.custom_ppo import CustomPPOTrainer
        from ray.rllib.agents.ppo import DEFAULT_CONFIG
        alg_run = CustomPPOTrainer
        config = deepcopy(DEFAULT_CONFIG)

        config["num_workers"] = n_cpus
        config["horizon"] = horizon
        config["model"].update({"fcnet_hiddens": [32, 32]})
        config["train_batch_size"] = horizon * n_rollouts
        config["gamma"] = 0.995  # discount rate
        config["use_gae"] = True
        config["no_done_at_end"] = True
        config["lambda"] = 0.97
        config["kl_target"] = 0.02
        config["num_sgd_iter"] = 10
        if flags.grid_search:
            config["lambda"] = tune.grid_search([0.5, 0.9])
            config["lr"] = tune.grid_search([5e-4, 5e-5])
    elif alg_run == "CENTRALIZEDPPO":
        from flow.algorithms.centralized_PPO import CCTrainer, CentralizedCriticModel
        from ray.rllib.agents.ppo import DEFAULT_CONFIG
        from ray.rllib.models import ModelCatalog
        alg_run = CCTrainer
        config = deepcopy(DEFAULT_CONFIG)
        config['model']['custom_model'] = "cc_model"
        config["model"]["custom_options"]["max_num_agents"] = flow_params['env'].additional_params['max_num_agents']
        config["model"]["custom_options"]["central_vf_size"] = 100

        ModelCatalog.register_custom_model("cc_model", CentralizedCriticModel)

        config["num_workers"] = n_cpus
        config["horizon"] = horizon
        config["model"].update({"fcnet_hiddens": [32, 32]})
        config["train_batch_size"] = horizon * n_rollouts
        config["gamma"] = 0.995  # discount rate
        config["use_gae"] = True
        config["lambda"] = 0.97
        config["kl_target"] = 0.02
        config["num_sgd_iter"] = 10
        if flags.grid_search:
            config["lambda"] = tune.grid_search([0.5, 0.9])
            config["lr"] = tune.grid_search([5e-4, 5e-5])

    elif alg_run == "TD3":
        alg_run = get_agent_class(alg_run)
        config = deepcopy(alg_run._default_config)

        config["num_workers"] = n_cpus
        config["horizon"] = horizon
        config["learning_starts"] = 10000
        config["buffer_size"] = 20000  # reduced to test if this is the source of memory problems
        if flags.grid_search:
            config["prioritized_replay"] = tune.grid_search(['True', 'False'])
            config["actor_lr"] = tune.grid_search([1e-3, 1e-4])
            config["critic_lr"] = tune.grid_search([1e-3, 1e-4])
            config["n_step"] = tune.grid_search([1, 10])

    else:
        sys.exit("We only support PPO, TD3, right now.")

    # define some standard and useful callbacks
    def on_episode_start(info):
        episode = info["episode"]
        episode.user_data["avg_speed"] = []
        episode.user_data["avg_speed_avs"] = []
        episode.user_data["avg_energy"] = []
        episode.user_data["inst_mpg"] = []
        episode.user_data["num_cars"] = []
        episode.user_data["avg_accel_human"] = []
        episode.user_data["avg_accel_avs"] = []

    def on_episode_step(info):
        episode = info["episode"]
        env = info["env"].get_unwrapped()[0]
        if isinstance(env, _GroupAgentsWrapper):
            env = env.env
        if hasattr(env, 'no_control_edges'):
            veh_ids = [
                veh_id for veh_id in env.k.vehicle.get_ids()
                if env.k.vehicle.get_speed(veh_id) >= 0
                and env.k.vehicle.get_edge(veh_id) not in env.no_control_edges
            ]
            rl_ids = [
                veh_id for veh_id in env.k.vehicle.get_rl_ids()
                if env.k.vehicle.get_speed(veh_id) >= 0
                and env.k.vehicle.get_edge(veh_id) not in env.no_control_edges
            ]
        else:
            veh_ids = [veh_id for veh_id in env.k.vehicle.get_ids() if env.k.vehicle.get_speed(veh_id) >= 0]
            rl_ids = [veh_id for veh_id in env.k.vehicle.get_rl_ids() if env.k.vehicle.get_speed(veh_id) >= 0]

        speed = np.mean([speed for speed in env.k.vehicle.get_speed(veh_ids)])
        if not np.isnan(speed):
            episode.user_data["avg_speed"].append(speed)
        av_speed = np.mean([speed for speed in env.k.vehicle.get_speed(rl_ids) if speed >= 0])
        if not np.isnan(av_speed):
            episode.user_data["avg_speed_avs"].append(av_speed)
        episode.user_data["inst_mpg"].append(instantaneous_mpg(env, veh_ids, gain=1.0))
        episode.user_data["num_cars"].append(len(env.k.vehicle.get_ids()))
        episode.user_data["avg_accel_human"].append(np.nan_to_num(np.mean(
            [np.abs((env.k.vehicle.get_speed(veh_id) - env.k.vehicle.get_previous_speed(veh_id))/env.sim_step) for
             veh_id in veh_ids if veh_id in env.k.vehicle.previous_speeds.keys()]
        )))
        episode.user_data["avg_accel_avs"].append(np.nan_to_num(np.mean(
            [np.abs((env.k.vehicle.get_speed(veh_id) - env.k.vehicle.get_previous_speed(veh_id))/env.sim_step) for
             veh_id in rl_ids if veh_id in env.k.vehicle.previous_speeds.keys()]
        )))

    def on_episode_end(info):
        episode = info["episode"]
        avg_speed = np.mean(episode.user_data["avg_speed"])
        episode.custom_metrics["avg_speed"] = avg_speed
        avg_speed_avs = np.mean(episode.user_data["avg_speed_avs"])
        episode.custom_metrics["avg_speed_avs"] = avg_speed_avs
        episode.custom_metrics["avg_accel_avs"] = np.mean(episode.user_data["avg_accel_avs"])
        episode.custom_metrics["avg_energy_per_veh"] = np.mean(episode.user_data["avg_energy"])
        episode.custom_metrics["avg_mpg_per_veh"] = np.mean(episode.user_data["inst_mpg"])
        episode.custom_metrics["num_cars"] = np.mean(episode.user_data["num_cars"])

    def on_train_result(info):
        """Store the mean score of the episode, and increment or decrement the iteration number for curriculum."""
        trainer = info["trainer"]
        trainer.workers.foreach_worker(
            lambda ev: ev.foreach_env(
                lambda env: env.set_iteration_num()))

    config["callbacks"] = {"on_episode_start": tune.function(on_episode_start),
                           "on_episode_step": tune.function(on_episode_step),
                           "on_episode_end": tune.function(on_episode_end),
                           "on_train_result": tune.function(on_train_result)}

    # save the flow params for replay
    flow_json = json.dumps(
        flow_params, cls=FlowParamsEncoder, sort_keys=True, indent=4)
    config['env_config']['flow_params'] = flow_json
    config['env_config']['run'] = alg_run

    # multiagent configuration
    if policy_graphs is not None:
        config['multiagent'].update({'policies': policy_graphs})
    if policy_mapping_fn is not None:
        config['multiagent'].update({'policy_mapping_fn': tune.function(policy_mapping_fn)})
    if policies_to_train is not None:
        config['multiagent'].update({'policies_to_train': policies_to_train})

    create_env, gym_name = make_create_env(params=flow_params)

    register_env(gym_name, create_env)
    return alg_run, gym_name, config


def train_rllib(submodule, flags):
    """Train policies using the PPO algorithm in RLlib."""
    import ray
    from ray import tune

    flow_params = submodule.flow_params
    flow_params['sim'].render = flags.render
    policy_graphs = getattr(submodule, "POLICY_GRAPHS", None)
    policy_mapping_fn = getattr(submodule, "policy_mapping_fn", None)
    policies_to_train = getattr(submodule, "policies_to_train", None)

    alg_run, gym_name, config = setup_exps_rllib(
        flow_params, flags.num_cpus, flags.num_rollouts, flags,
        policy_graphs, policy_mapping_fn, policies_to_train)

    config['num_workers'] = flags.num_cpus
    config['env'] = gym_name

    # create a custom string that makes looking at the experiment names easier
    def trial_str_creator(trial):
        return "{}_{}".format(trial.trainable_name, trial.experiment_tag)

    if flags.multi_node:
        ray.init(redis_address='localhost:6379')
    elif flags.local_mode:
        ray.init(local_mode=True)
    else:
        ray.init()
    exp_dict = {
        "run_or_experiment": alg_run,
        "name": flags.exp_title or flow_params['exp_tag'],
        "config": config,
        "checkpoint_freq": flags.checkpoint_freq,
        "checkpoint_at_end": True,
        'trial_name_creator': trial_str_creator,
        "max_failures": 0,
        "stop": {
            "training_iteration": flags.num_iterations,
        },
    }
    date = datetime.now(tz=pytz.utc)
    date = date.astimezone(pytz.timezone('US/Pacific')).strftime("%m-%d-%Y")
    if flags.use_s3:
        s3_string = "s3://i210.experiments/i210/" \
                    + date + '/' + flags.exp_title
        exp_dict['upload_dir'] = s3_string
    tune.run(**exp_dict, queue_trials=False, raise_on_failed_trial=False)

    if flags.upload_graphs:
        print('Generating experiment graphs and uploading them to leaderboard')
        submitter_name, strategy_name = flags.upload_graphs

        # reset ray
        ray.shutdown()
        if flags.local_mode:
            ray.init(local_mode=True)
        else:
            ray.init()

        # grab checkpoint path
        for (dirpath, _, _) in os.walk(os.path.expanduser("~/ray_results")):
            if "checkpoint_{}".format(flags.checkpoint_freq) in dirpath \
               and dirpath.split('/')[-3] == flags.exp_title:
                checkpoint_path = os.path.dirname(dirpath)
                checkpoint_number = -1
                for name in os.listdir(checkpoint_path):
                    if name.startswith('checkpoint'):
                        cp = int(name.split('_')[1])
                        checkpoint_number = max(checkpoint_number, cp)

                # create dir for graphs output
                output_dir = os.path.join(checkpoint_path, 'output_graphs')
                if not os.path.exists(output_dir):
                    os.mkdir(output_dir)

                # run graph generation script
                parser = create_parser()

                strategy_name_full = str(strategy_name)
                if flags.grid_search:
                    strategy_name_full += '__' + dirpath.split('/')[-2]

                args = parser.parse_args([
                    '-r', checkpoint_path, '-c', str(checkpoint_number),
                    '--gen_emission', '--use_s3', '--num_cpus', str(flags.num_cpus),
                    '--output_dir', output_dir,
                    '--submitter_name', submitter_name,
                    '--strategy_name', strategy_name_full.replace(',', '_').replace(';', '_')
                ])
                generate_graphs(args)


def train_h_baselines(env_name, args, multiagent):
    """Train policies using SAC and TD3 with h-baselines."""
    from hbaselines.algorithms import OffPolicyRLAlgorithm
    from hbaselines.utils.train import parse_options, get_hyperparameters

    # Get the command-line arguments that are relevant here
    args = parse_options(description="", example_usage="", args=args)

    # the base directory that the logged data will be stored in
    base_dir = "training_data"

    for i in range(args.n_training):
        # value of the next seed
        seed = args.seed + i

        # The time when the current experiment started.
        now = strftime("%Y-%m-%d-%H:%M:%S")

        # Create a save directory folder (if it doesn't exist).
        dir_name = os.path.join(base_dir, '{}/{}'.format(args.env_name, now))
        ensure_dir(dir_name)

        # Get the policy class.
        if args.alg == "TD3":
            if multiagent:
                from hbaselines.multi_fcnet.td3 import MultiFeedForwardPolicy
                policy = MultiFeedForwardPolicy
            else:
                from hbaselines.fcnet.td3 import FeedForwardPolicy
                policy = FeedForwardPolicy
        elif args.alg == "SAC":
            if multiagent:
                from hbaselines.multi_fcnet.sac import MultiFeedForwardPolicy
                policy = MultiFeedForwardPolicy
            else:
                from hbaselines.fcnet.sac import FeedForwardPolicy
                policy = FeedForwardPolicy
        else:
            raise ValueError("Unknown algorithm: {}".format(args.alg))

        # Get the hyperparameters.
        hp = get_hyperparameters(args, policy)

        # Add the seed for logging purposes.
        params_with_extra = hp.copy()
        params_with_extra['seed'] = seed
        params_with_extra['env_name'] = args.env_name
        params_with_extra['policy_name'] = policy.__name__
        params_with_extra['algorithm'] = args.alg
        params_with_extra['date/time'] = now

        # Add the hyperparameters to the folder.
        with open(os.path.join(dir_name, 'hyperparameters.json'), 'w') as f:
            json.dump(params_with_extra, f, sort_keys=True, indent=4)

        # Create the algorithm object.
        alg = OffPolicyRLAlgorithm(
            policy=policy,
            env="flow:{}".format(env_name),
            eval_env="flow:{}".format(env_name) if args.evaluate else None,
            **hp
        )

        # Perform training.
        alg.learn(
            total_steps=args.total_steps,
            log_dir=dir_name,
            log_interval=args.log_interval,
            eval_interval=args.eval_interval,
            save_interval=args.save_interval,
            initial_exploration_steps=args.initial_exploration_steps,
            seed=seed,
        )


def train_stable_baselines(submodule, flags):
    """Train policies using the PPO algorithm in stable-baselines."""
    from stable_baselines.common.vec_env import DummyVecEnv
    from stable_baselines import PPO2
    flow_params = submodule.flow_params
    # Path to the saved files
    exp_tag = flow_params['exp_tag']
    result_name = '{}/{}'.format(exp_tag, strftime("%Y-%m-%d-%H:%M:%S"))

    # Perform training.
    print('Beginning training.')
    model = run_model_stablebaseline(
        flow_params, flags.num_cpus, flags.rollout_size, flags.num_steps)

    # Save the model to a desired folder and then delete it to demonstrate
    # loading.
    print('Saving the trained model!')
    path = os.path.realpath(os.path.expanduser('~/baseline_results'))
    ensure_dir(path)
    save_path = os.path.join(path, result_name)
    model.save(save_path)

    # dump the flow params
    with open(os.path.join(path, result_name) + '.json', 'w') as outfile:
        json.dump(flow_params, outfile,
                  cls=FlowParamsEncoder, sort_keys=True, indent=4)

    # Replay the result by loading the model
    print('Loading the trained model and testing it out!')
    model = PPO2.load(save_path)
    flow_params = get_flow_params(os.path.join(path, result_name) + '.json')
    flow_params['sim'].render = True
    env = env_constructor(params=flow_params, version=0)()
    # The algorithms require a vectorized environment to run
    eval_env = DummyVecEnv([lambda: env])
    obs = eval_env.reset()
    reward = 0
    for _ in range(flow_params['env'].horizon):
        action, _states = model.predict(obs)
        obs, rewards, dones, info = eval_env.step(action)
        reward += rewards
    print('the final reward is {}'.format(reward))


def main(args):
    """Perform the training operations."""
    # Parse script-level arguments (not including package arguments).
    flags = parse_args(args)

    # Import relevant information from the exp_config script.
    module = __import__(
        "exp_configs.rl.singleagent", fromlist=[flags.exp_config])
    module_ma = __import__(
        "exp_configs.rl.multiagent", fromlist=[flags.exp_config])

    # Import the sub-module containing the specified exp_config and determine
    # whether the environment is single agent or multi-agent.
    if hasattr(module, flags.exp_config):
        submodule = getattr(module, flags.exp_config)
        multiagent = False
    elif hasattr(module_ma, flags.exp_config):
        submodule = getattr(module_ma, flags.exp_config)
        assert flags.rl_trainer.lower() in ["rllib", "h-baselines"], \
            "Currently, multiagent experiments are only supported through "\
            "RLlib. Try running this experiment using RLlib: " \
            "'python train.py EXP_CONFIG'"
        multiagent = True
    else:
        raise ValueError("Unable to find experiment config.")

    # Perform the training operation.
    if flags.rl_trainer.lower() == "rllib":
        train_rllib(submodule, flags)
    elif flags.rl_trainer.lower() == "stable-baselines":
        train_stable_baselines(submodule, flags)
    elif flags.rl_trainer.lower() == "h-baselines":
        train_h_baselines(flags.exp_config, args, multiagent)
    else:
        raise ValueError("rl_trainer should be either 'rllib', 'h-baselines', "
                         "or 'stable-baselines'.")


if __name__ == "__main__":
    main(sys.argv[1:])
