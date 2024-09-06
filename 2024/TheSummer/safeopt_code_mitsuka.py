from typing import Any
from typing import Callable
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Union
import warnings

import numpy
import torch
from torch import Tensor
from packaging import version

from optuna import logging
from optuna._experimental import experimental_class
from optuna._experimental import experimental_func
from optuna._imports import try_import
from optuna._transform import _SearchSpaceTransform
from optuna.distributions import BaseDistribution
from optuna.samplers import BaseSampler
from optuna.samplers import RandomSampler
from optuna.samplers._base import _CONSTRAINTS_KEY
from optuna.samplers._base import _process_constraints_after_trial
from optuna.search_space import IntersectionSearchSpace
from optuna.study import Study
from optuna.study import StudyDirection
from optuna.trial import FrozenTrial
from optuna.trial import TrialState
import proximal
import sys
import configparser
from epics import PV, caget, caput
import matplotlib.pyplot as plt



from botorch.acquisition.monte_carlo import qExpectedImprovement
from botorch.acquisition.monte_carlo import qNoisyExpectedImprovement
from botorch.acquisition.multi_objective import monte_carlo
from botorch.acquisition.multi_objective.objective import (
    IdentityMCMultiOutputObjective,
)
from botorch.acquisition.objective import ConstrainedMCObjective
from botorch.acquisition.objective import GenericMCObjective
from botorch.models import SingleTaskGP, ModelListGP
from botorch.models.transforms.outcome import Standardize
from botorch.optim import optimize_acqf
from botorch.sampling import SobolQMCNormalSampler
import botorch.version
from botorch.acquisition import ExpectedImprovement, UpperConfidenceBound
from botorch.acquisition import qUpperConfidenceBound

if version.parse(botorch.version.version) < version.parse("0.8.0"):
    from botorch.fit import fit_gpytorch_model

    def _get_sobol_qmc_normal_sampler(num_samples: int) -> SobolQMCNormalSampler:
        return SobolQMCNormalSampler(num_samples)

else:
    from botorch.fit import fit_gpytorch_mll

    def _get_sobol_qmc_normal_sampler(num_samples: int) -> SobolQMCNormalSampler:
        return SobolQMCNormalSampler(torch.Size((num_samples,)))

from botorch.utils.multi_objective.box_decompositions import (
    NondominatedPartitioning,
)
from botorch.utils.multi_objective.scalarization import get_chebyshev_scalarization
from botorch.utils.sampling import manual_seed
from botorch.utils.sampling import sample_simplex
from botorch.utils.transforms import normalize, unnormalize
from botorch.models.transforms import Standardize
from gpytorch.mlls import ExactMarginalLogLikelihood, SumMarginalLogLikelihood


_logger = logging.get_logger(__name__)

def constrained_candidates_func(
    train_x: torch.Tensor,
    train_obj: torch.Tensor,
    train_con: Optional[torch.Tensor],
    bounds: torch.Tensor,
    pending_x: torch.Tensor = None,
    acquisition_type_obj: str = "EI",
    acquisition_type_con: str = "EI",
    beta_obj: float = 2.0,
    beta_con: float = 2.0,
    constraint_threshold: float = 0.5,
    fig=None, ax=None,
) -> torch.Tensor:
    
    if train_con is not None:
        print("制約付きの候補点生成を行っています...")
    else:
        print("制約なしの候補点生成を行っています...")

    train_x = normalize(train_x, bounds=bounds)

    # 目的関数のためのGPモデルを作成
    model_obj = SingleTaskGP(train_x, train_obj, outcome_transform=Standardize(m=train_obj.size(-1)))
    mll_obj   = ExactMarginalLogLikelihood(model_obj.likelihood, model_obj)
    fit_gpytorch_mll(mll_obj)    
    acqf_obj = ExpectedImprovement(model=model_obj, best_f=train_obj.max())

    # 制約がある場合、制約のためのGPモデルを作成
    #train_con = normalize(train_con, bounds=torch.tensor([[0.0], [1.0]]).to(train_con.device))
    model_con = SingleTaskGP(train_x, train_con, outcome_transform=Standardize(m=train_con.size(-1)))
    mll_con = ExactMarginalLogLikelihood(model_con.likelihood, model_con)
    fit_gpytorch_mll(mll_con)
    acqf_con = ExpectedImprovement(model=model_con, best_f=train_con.min())

    standard_bounds = torch.zeros_like(bounds)
    standard_bounds[1] = 1

    grid_size = 50
    x = torch.linspace(0, 1, grid_size)
    y = torch.linspace(0, 1, grid_size)
    X, Y = torch.meshgrid(x, y)
    XY = torch.stack([X.ravel(), Y.ravel()], dim=-1).to(train_x.device)
    XY = XY.unsqueeze(1)
    
    with torch.no_grad():
        acq_values_obj = acqf_obj(XY).reshape(grid_size, grid_size).detach().cpu().numpy()
        acq_values_con = acqf_con(XY).reshape(grid_size, grid_size).detach().cpu().numpy()

    from botorch.acquisition.objective import ConstrainedMCObjective

    # define a feasibility-weighted objective for optimization
    constrained_obj = ConstrainedMCObjective(
        objective=lambda Z: Z[..., 0],
        constraints=[lambda Z: Z[..., 1]],
    )

    models = ModelListGP(model_obj, model_con)  #複数のモデルを扱うためModelListを使う
    mll = SumMarginalLogLikelihood(models.likelihood, models)
    fit_gpytorch_mll(mll)  #model_objとmodel_conのハイパーパラメータの最適化

    qEI = qExpectedImprovement(model=models, best_f=(train_obj * (train_con <= 0.1)).max(), objective=constrained_obj,sampler=SobolQMCNormalSampler(sample_shape=torch.Size([1024])),)
    #qEI = UpperConfidenceBound(model=model_obj, beta=beta_obj)
    #qEI = qExpectedImprovement(model=models, best_f=train_obj.max())
    # constrained_acqf(X) の結果を計算して最大値を見つける
    constrained_acq_values = qEI(XY).reshape(grid_size, grid_size).detach().cpu().numpy()
    max_idx_constrained = constrained_acq_values.argmax()

    max_x_constrained, max_y_constrained = X.ravel()[max_idx_constrained], Y.ravel()[max_idx_constrained]

    try:
        candidates, acq_value = optimize_acqf(
            acq_function=qEI,
            bounds=standard_bounds,
            q=1,
            num_restarts=10,
            raw_samples=512,
            options={"batch_limit": 5, "maxiter": 200, "nonnegative": True},
            sequential=True,
        )
        best_candidate = unnormalize(candidates.detach(), bounds=bounds)
        print(acq_value)
    except RuntimeError as e:
        print("RuntimeError encountered:", e)
        raise e

    if fig is not None:
        plt.close(fig)

    fig, ax = plt.subplots(1, 3, figsize=(24, 6))

    # 目的関数の獲得関数の最大値をプロット
    obj_acq_contour = ax[0].contourf(X.numpy(), Y.numpy(), acq_values_obj, levels=50, cmap = "plasma")
    ax[0].scatter(train_x[:, 0].cpu().numpy(), train_x[:, 1].cpu().numpy(), color="white", marker="x", label="Training Data")
    #ax[0].scatter(max_x_obj, max_y_obj, color="blue", marker="o", s=100, label="Max Value")
    ax[0].set_title("Objective Function (Acquisition)")
    ax[0].set_xlabel("X1")
    ax[0].set_ylabel("X2")
    fig.colorbar(obj_acq_contour, ax=ax[0])

    # 制約の獲得関数の最大値をプロット
    if acqf_con is not None:
        con_acq_contour = ax[1].contourf(X.numpy(), Y.numpy(), acq_values_con, levels=50, cmap = "plasma")
        ax[1].scatter(train_x[:, 0].cpu().numpy(), train_x[:, 1].cpu().numpy(), color="white", marker="x", label="Training Data")
        #ax[1].scatter(max_x_con, max_y_con, color="blue", marker="o", s=100, label="Max Value")
        ax[1].set_title("Constraint Function (Acquisition)")
        ax[1].set_xlabel("X1")
        ax[1].set_ylabel("X2")
        fig.colorbar(con_acq_contour, ax=ax[1])

    # 制約付き獲得関数の最大値をプロット
    constrained_acq_contour = ax[2].contourf(X.numpy(), Y.numpy(), constrained_acq_values, levels=50, cmap = "plasma")
    ax[2].scatter(train_x[:, 0].cpu().numpy(), train_x[:, 1].cpu().numpy(), color="white", marker="x", label="Training Data")
    ax[2].scatter(max_x_constrained, max_y_constrained, color="cyan", marker="o", s=100, label="Max Value")
    ax[2].set_title("Constrained Acquisition Function")
    ax[2].set_xlabel("X1")
    ax[2].set_ylabel("X2")
    ax[2].legend()
    fig.colorbar(constrained_acq_contour, ax=ax[2])

    plt.tight_layout()
    plt.show()
    plt.pause(0.01)

    return best_candidate





@experimental_class("2.4.0")
class SafeOptSampler(BaseSampler):
    """A sampler that uses BoTorch, a Bayesian optimization library built on top of PyTorch.

    This sampler allows using BoTorch's optimization algorithms from Optuna to suggest parameter
    configurations. Parameters are transformed to continuous space and passed to BoTorch, and then
    transformed back to Optuna's representations. Categorical parameters are one-hot encoded.

    .. seealso::
        See an `example <https://github.com/optuna/optuna-examples/blob/main/multi_objective/
        botorch_simple.py>`_ how to use the sampler.

    .. seealso::
        See the `BoTorch <https://botorch.org/>`_ homepage for details and for how to implement
        your own ``candidates_func``.

    .. note::
        An instance of this sampler *should not be used with different studies* when used with
        constraints. Instead, a new instance should be created for each new study. The reason for
        this is that the sampler is stateful keeping all the computed constraints.

    Args:
        candidates_func:
            An optional function that suggests the next candidates. It must take the training
            data, the objectives, the constraints, the search space bounds and return the next
            candidates. The arguments are of type ``torch.Tensor``. The return value must be a
            ``torch.Tensor``. However, if ``constraints_func`` is omitted, constraints will be
            :obj:`None`. For any constraints that failed to compute, the tensor will contain
            NaN.

            If omitted, it is determined automatically based on the number of objectives and
            whether a constraint is specified. If the
            number of objectives is one and no constraint is specified, log-Expected Improvement
            is used. If constraints are specified, quasi MC-based batch Expected Improvement
            (qEI) is used.
            If the number of objectives is either two or three, Quasi MC-based
            batch Expected Hypervolume Improvement (qEHVI) is used. Otherwise, for larger number
            of objectives, the faster Quasi MC-based extended ParEGO (qParEGO) is used.

            The function should assume *maximization* of the objective.

            .. seealso::
                See :func:`optuna.integration.botorch.qei_candidates_func` for an example.
        constraints_func:
            An optional function that computes the objective constraints. It must take a
            :class:`~optuna.trial.FrozenTrial` and return the constraints. The return value must
            be a sequence of :obj:`float` s. A value strictly larger than 0 means that a
            constraint is violated. A value equal to or smaller than 0 is considered feasible.

            If omitted, no constraints will be passed to ``candidates_func`` nor taken into
            account during suggestion.
        n_startup_trials:
            Number of initial trials, that is the number of trials to resort to independent
            sampling.
        consider_running_trials:
            If True, the acquisition function takes into consideration the running parameters
            whose evaluation has not completed. Enabling this option is considered to improve the
            performance of parallel optimization.

            .. note::
                Added in v3.2.0 as an experimental argument.
        independent_sampler:
            An independent sampler to use for the initial trials and for parameters that are
            conditional.
        seed:
            Seed for random number generator.
        device:
            A ``torch.device`` to store input and output data of BoTorch. Please set a CUDA device
            if you fasten sampling.
    """

    def __init__(
        self,
        x_name_list: list,
        x_min_max_list: list,
        x_weight_list: list,
        UCB_obj: bool,         # 目的関数のUCBフラグ
        EI_obj: bool,          # 目的関数のEIフラグ
        beta_obj: float,       # 目的関数のbeta値
        UCB_con: bool,         # 制約のUCBフラグ
        EI_con: bool,          # 制約のEIフラグ
        beta_con: float,       # 制約のbeta値
        constraint_threshold: float, #制約の閾値
        *,
        candidates_func: Optional[
            Callable[
                [
                    "torch.Tensor",  # train_x
                    "torch.Tensor",  # train_obj
                    Optional["torch.Tensor"],  # train_con
                    "torch.Tensor",  # bounds
                    Optional["torch.Tensor"],  # pending_x
                    str,            # acquisition_type_obj
                    str,            # acquisition_type_con
                    float,          # beta_obj
                    float,          # beta_con
                    float           # constraint_threshold
                ],
                "torch.Tensor",
            ]
        ] = None,
        constraints_func: Optional[Callable[[FrozenTrial], Sequence[float]]] = None,
        n_startup_trials: int = 10,
        consider_running_trials: bool = False,
        independent_sampler: Optional[BaseSampler] = None,
        seed: Optional[int] = None,
        device: Optional["torch.device"] = None,
    ):
        self.x_name_list = x_name_list
        self.x_min_max_list = x_min_max_list
        self.x_weight_list = x_weight_list
        self.UCB_obj = UCB_obj
        self.EI_obj = EI_obj
        self.beta_obj = beta_obj
        self.UCB_con = UCB_con
        self.EI_con = EI_con
        self.beta_con = beta_con
        self.constraint_threshold = constraint_threshold

        self._candidates_func = candidates_func or constrained_candidates_func
        self._constraints_func = constraints_func
        self._consider_running_trials = consider_running_trials
        self._independent_sampler = independent_sampler or RandomSampler(seed=seed)
        self._n_startup_trials = n_startup_trials
        self._seed = seed

        self._study_id: Optional[int] = None
        self._search_space = IntersectionSearchSpace()
        self._device = device or torch.device("cpu")

    def infer_relative_search_space(
        self,
        study: Study,
        trial: FrozenTrial,
    ) -> Dict[str, BaseDistribution]:
        if self._study_id is None:
            self._study_id = study._study_id
        if self._study_id != study._study_id:
            # Note that the check below is meaningless when `InMemoryStorage` is used
            # because `InMemoryStorage.create_new_study` always returns the same study ID.
            raise RuntimeError("BoTorchSampler cannot handle multiple studies.")

        search_space: Dict[str, BaseDistribution] = {}
        for name, distribution in self._search_space.calculate(study).items():
            if distribution.single():
                # built-in `candidates_func` cannot handle distributions that contain just a
                # single value, so we skip them. Note that the parameter values for such
                # distributions are sampled in `Trial`.
                continue
            search_space[name] = distribution

        return search_space

    def sample_relative(
        self,
        study: Study,
        trial: FrozenTrial,
        search_space: Dict[str, BaseDistribution],
    ) -> Dict[str, Any]:
        assert isinstance(search_space, dict)

        if len(search_space) == 0:
            return {}

        completed_trials = study.get_trials(
            deepcopy=False, states=(TrialState.COMPLETE,)
        )

        n_completed_trials = len(completed_trials)
        con = None
        for trial_idx, trial in enumerate(completed_trials):
            constraints = trial.user_attrs.get("constraints")
            if constraints is not None:
                n_constraints = len(constraints)

                if con is None:
                    con = torch.full(
                        (n_completed_trials, n_constraints),
                        float('nan'),
                        dtype=torch.float64,  # torch.float64を指定
                        device=self._device
                    )

                con[trial_idx] = torch.tensor(constraints, dtype=torch.float64, device=self._device)
                
        #con = None  #debug

        running_trials = [
            t
            for t in study.get_trials(deepcopy=False, states=(TrialState.RUNNING,))
            if t != trial
        ]
        trials = completed_trials + running_trials

        n_trials = len(trials)
        if n_trials < self._n_startup_trials:
            return {}

        trans = _SearchSpaceTransform(search_space)
        n_objectives = len(study.directions)
        values: Union[numpy.ndarray, torch.Tensor] = numpy.empty(
            (n_trials, n_objectives), dtype=numpy.float64  # dtypeをfloat64に変更
        )
        params: Union[numpy.ndarray, torch.Tensor]
        bounds: Union[numpy.ndarray, torch.Tensor] = trans.bounds.astype(numpy.float64)  # float64に変換
        params = numpy.empty((n_trials, trans.bounds.shape[0]), dtype=numpy.float64)  # dtypeをfloat64に変更

        for trial_idx, trial in enumerate(trials):
            if trial.state == TrialState.COMPLETE:
                params[trial_idx] = trans.transform(trial.params).astype(numpy.float64)  # float64に変換
                assert len(study.directions) == len(trial.values)
                for obj_idx, (direction, value) in enumerate(
                    zip(study.directions, trial.values)
                ):
                    assert value is not None
                    if (
                        direction == StudyDirection.MINIMIZE
                    ):  # BoTorch always assumes maximization.
                        value *= -1
                    values[trial_idx, obj_idx] = value
            elif trial.state == TrialState.RUNNING:
                if all(p in trial.params for p in search_space):
                    params[trial_idx] = trans.transform(trial.params).astype(numpy.float64)  # float64に変換
                else:
                    params[trial_idx] = numpy.nan
            else:
                assert (
                    False
                ), "trial.state must be TrialState.COMPLETE or TrialState.RUNNING."

        values = torch.from_numpy(values).to(self._device)
        params = torch.from_numpy(params).to(self._device)
        bounds = torch.from_numpy(bounds).to(self._device)
        bounds.transpose_(0, 1)

        if self._candidates_func is None:
            self._candidates_func = constrained_candidates_func(
                n_objectives=n_objectives,
                has_constraint=con is not None,
                consider_running_trials=self._consider_running_trials,
            )

        completed_values = values[:n_completed_trials]
        completed_params = params[:n_completed_trials]
        if self._consider_running_trials:
            running_params = params[n_completed_trials:]
            running_params = running_params[~torch.isnan(running_params).any(dim=1)]
        else:
            running_params = None

        with manual_seed(self._seed):
            candidates = self._candidates_func(
                completed_params,          # train_x
                completed_values,          # train_obj
                con,                       # train_con
                bounds,                    # bounds
                pending_x=running_params,  # pending_x (optional)
                acquisition_type_obj="EI" if self.EI_obj else "UCB",  # acquisition_type_obj
                acquisition_type_con="EI" if self.EI_con else "UCB",  # acquisition_type_con
                beta_obj=self.beta_obj,    # 目的関数用のbeta値
                beta_con=self.beta_con,    # 制約用のbeta値
                constraint_threshold=self.constraint_threshold   # constraint_threshold (適宜変更可能)
            )
            if self._seed is not None:
                self._seed += 1

        if not isinstance(candidates, torch.Tensor):
            raise TypeError("Candidates must be a torch.Tensor.")
        if candidates.dim() == 2:
            if candidates.size(0) != 1:
                raise ValueError(
                    "Candidates batch optimization is not supported and the first dimension must "
                    "have size 1 if candidates is a two-dimensional tensor. Actual: "
                    f"{candidates.size()}."
                )
            # Batch size is one. Get rid of the batch dimension.
            candidates = candidates.squeeze(0)
        if candidates.dim() != 1:
            raise ValueError("Candidates must be one or two-dimensional.")
        if candidates.size(0) != bounds.size(1):
            raise ValueError(
                "Candidates size must match with the given bounds. Actual candidates: "
                f"{candidates.size(0)}, bounds: {bounds.size(1)}."
            )

        return trans.untransform(candidates.cpu().numpy())

    def sample_independent(
        self,
        study: Study,
        trial: FrozenTrial,
        param_name: str,
        param_distribution: BaseDistribution,
    ) -> Any:
        return self._independent_sampler.sample_independent(
            study, trial, param_name, param_distribution
        )

    def reseed_rng(self) -> None:
        self._independent_sampler.reseed_rng()
        if self._seed is not None:
            self._seed = numpy.random.RandomState().randint(
                numpy.iinfo(numpy.int64).max
            )

    def after_trial(
        self,
        study: Study,
        trial: FrozenTrial,
        state: TrialState,
        values: Optional[Sequence[float]],
    ) -> None:
        if self._constraints_func is not None:
            _process_constraints_after_trial(
                self._constraints_func, study, trial, state
            )
        self._independent_sampler.after_trial(study, trial, state, values)


