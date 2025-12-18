"""
饮料生产企业线性规划优化模型
运筹学专家系统 - 解决原料和运输双重约束下的利润最大化问题
"""

import numpy as np
import pandas as pd
from scipy.optimize import linprog  # HiGHS solver entry point for large LP problems

# Optional visualization stacks keep the solver usable in batch/headless environments
# 注：模型核心功能与 Streamlit、Plotly 等可视化库解耦。
# 这些依赖在模型求解过程中并不会用到，但如果在其他模块中需要可视化时可单独引入。
# 为避免在无可视化环境下运行测试时引发 ImportError，这里将其设为可选导入。
try:
    import streamlit as st  # type: ignore
except ImportError:
    st = None  # 在模型逻辑中不会用到

try:
    import plotly.graph_objects as go  # type: ignore
    import plotly.express as px  # type: ignore
    from plotly.subplots import make_subplots  # type: ignore
except ImportError:
    go = px = make_subplots = None  # 可视化仅在 Streamlit 应用中使用
import json
from typing import Any, Dict, List, Tuple, Optional
import warnings

# Silence repeated numerical warnings so the notebook/Streamlit console remains readable
warnings.filterwarnings('ignore')

class BeverageOptimizationModel:
    """
    饮料生产企业线性规划优化模型类
    
    该类构建了一个完整的线性规划模型，用于解决饮料生产企业在原料供应和运输能力
    双重约束条件下的利润最大化问题。
    """
    
    def __init__(self):
        """初始化模型参数"""
        # 定义饮料种类和相关参数
        # Production decision variables ordered by SKU importance
        self.beverage_types = ['碳酸饮料', '果汁饮料', '茶饮料', '功能饮料', '矿泉水']
        # Cache counts to reuse when building matrix dimensions
        self.n_beverages = len(self.beverage_types)
        
        # 定义原料种类
        # Raw-material categories whose stock levels constrain production
        self.material_types = ['白砂糖', '浓缩果汁', '茶叶提取物', '功能成分', '包装材料']
        # Track how many material constraints must be generated
        self.n_materials = len(self.material_types)
        
        # 定义运输区域
        # Downstream distribution regions with dedicated transport quotas
        self.transport_regions = ['道里区', '南岗区', '道外区', '香坊区', '松北区']
        # Same idea for transport capacity constraints
        self.n_regions = len(self.transport_regions)
        
        # 初始化默认参数
        # Load a baseline data pack so the model can be solved immediately
        self.setup_default_parameters()
        
    def setup_default_parameters(self):
        """设置默认模型参数"""
        
        # 1. 利润参数 (元/升)
        # Unit profit (per thousand liters) directly feeds the objective vector
        self.profits = np.array([8.5, 12.0, 10.5, 15.0, 6.0])  # 各饮料单位利润
        
        # 2. 原料消耗矩阵 (单位: 千克/升)
        # 每行代表一种原料，每列代表一种饮料
        # Material usage matrix: rows=raw materials, cols=beverage SKUs
        self.material_consumption = np.array([
            [0.15, 0.08, 0.06, 0.10, 0.02],  # 白砂糖
            [0.02, 0.25, 0.03, 0.05, 0.01],  # 浓缩果汁
            [0.01, 0.02, 0.20, 0.08, 0.01],  # 茶叶提取物
            [0.00, 0.00, 0.00, 0.15, 0.00],  # 功能成分
            [0.10, 0.12, 0.11, 0.14, 0.08]   # 包装材料
        ])
        
        # 3. 原料供应限制 (千克)
        # Available inventory per raw material for the planning cycle
        # Regional supply estimates derived from 2025 Harbin industrial planning brief (in tons)
        self.material_limits = np.array([15000, 8000, 6000, 2000, 12000])
        
        # 4. 运输能力限制 (升)
        # Upper bounds for shipping capacity into each sales region (in kl)
        # Freight capacities (general + cold-chain) per district, unit: thousand liters
        self.transport_limits = np.array([3000, 2500, 2000, 1800, 1200])
        
        # 5. 各饮料在各区域的需求量权重
        # Demand allocation ratios telling us how production is split into regions
        self.demand_weights = np.array([
            [0.25, 0.30, 0.20, 0.15, 0.10],  # 碳酸饮料
            [0.20, 0.35, 0.25, 0.15, 0.05],  # 果汁饮料
            [0.30, 0.25, 0.20, 0.20, 0.05],  # 茶饮料
            [0.35, 0.30, 0.20, 0.10, 0.05],  # 功能饮料
            [0.15, 0.25, 0.30, 0.20, 0.10]   # 矿泉水
        ])
        
        # 6. 上期销售情况 (升)
        # Historical sales baseline, used for both min and max production guardrails
        self.previous_sales = np.array([2000, 1500, 1200, 800, 2500])
        
        # 7. 最小生产量要求 (销售量的80%)
        # Enforce at least 80% of last period output to keep shelves supplied
        self.min_production = 0.8 * self.previous_sales
        
        # 8. 最大生产能力限制
        # Cap any SKU at 150% of historical sales to avoid unrealistic spikes
        self.max_production_multiplier = 1.5
        
    def build_matrices(self):
        """
        构建线性规划的标准形式矩阵
        
        目标函数: max c^T x
        约束条件: Ax <= b
        """
        
        # 决策变量: x = [x1, x2, x3, x4, x5] 各饮料生产量
        
        # 目标函数系数 (最大化问题需要转换为最小化)
        # Keep decision variables ordered exactly as self.beverage_types for easier analysis
        c = -self.profits  # 负号因为linprog默认最小化
        # SciPy solves a minimization problem, so flipping the sign yields a max objective
        
        # 约束矩阵 A 和约束向量 b
        constraint_list = []
        constraint_rhs = []
        # Build A and b incrementally so that new business rules are easy to append
        
        # 1. 原料约束
        for i in range(self.n_materials):
            constraint_list.append(self.material_consumption[i, :])
            constraint_rhs.append(self.material_limits[i])
            # 每个原料的消耗行都与其库存上限匹配，形成 n_materials 条约束
        
        # 2. 运输能力约束
        # 总运输量不能超过各区域的运输能力
        for j in range(self.n_regions):
            # 计算各饮料在该区域的运输量
            transport_constraint = np.zeros(self.n_beverages)
            for i in range(self.n_beverages):
                # 假设生产量按需求权重分配到各区域
                transport_constraint[i] = self.demand_weights[i, j]
            constraint_list.append(transport_constraint)
            constraint_rhs.append(self.transport_limits[j])
            # 将产量按需求权重分配到各区域后，与对应运输能力比较
        
        # 3. 最小生产量约束
        for i in range(self.n_beverages):
            min_constraint = np.zeros(self.n_beverages)
            min_constraint[i] = -1  # -x_i <= -min_production_i
            constraint_list.append(min_constraint)
            constraint_rhs.append(-self.min_production[i])
            # 负号将 >= 约束转化成 <= 形式，便于传给 linprog
        
        # 4. 最大生产能力约束
        for i in range(self.n_beverages):
            max_constraint = np.zeros(self.n_beverages)
            max_constraint[i] = 1
            constraint_list.append(max_constraint)
            constraint_rhs.append(self.max_production_multiplier * self.previous_sales[i])
        # 简单的 <= 约束，防止任何 SKU 超过历史销量的 1.5 倍

        A = np.array(constraint_list)
        b = np.array(constraint_rhs)

        return c, A, b  # Standard form consumed directly by SciPy

    def build_constraint_records(self) -> List[Dict[str, np.ndarray]]:
        """构建带有方向信息的约束，供单纯形迭代表可视化使用。"""
        records: List[Dict[str, np.ndarray]] = []

        for idx, material in enumerate(self.material_types):
            records.append({
                'label': f"原料-{material}",
                'sense': '<=',
                'rhs': float(self.material_limits[idx]),
                'coeffs': self.material_consumption[idx, :].astype(float)
            })

        for idx, region in enumerate(self.transport_regions):
            coeffs = self.demand_weights[:, idx].astype(float)
            records.append({
                'label': f"运输-{region}",
                'sense': '<=',
                'rhs': float(self.transport_limits[idx]),
                'coeffs': coeffs
            })

        for idx, beverage in enumerate(self.beverage_types):
            coeffs = np.zeros(self.n_beverages)
            coeffs[idx] = 1.0
            records.append({
                'label': f"最小产量-{beverage}",
                'sense': '>=',
                'rhs': float(self.min_production[idx]),
                'coeffs': coeffs
            })

        for idx, beverage in enumerate(self.beverage_types):
            coeffs = np.zeros(self.n_beverages)
            coeffs[idx] = 1.0
            records.append({
                'label': f"最大产量-{beverage}",
                'sense': '<=',
                'rhs': float(self.max_production_multiplier * self.previous_sales[idx]),
                'coeffs': coeffs
            })

        return records

    def generate_simplex_iterations(self) -> Optional[Dict[str, Any]]:
        """生成单纯形法迭代的详细记录，便于前端逐步展示。"""
        try:
            constraint_records = self.build_constraint_records()
            if not constraint_records:
                return None
            return self._run_logged_simplex(constraint_records)
        except Exception as exc:  # pragma: no cover
            import traceback
            return {'error': str(exc), 'traceback': traceback.format_exc()}

    def _run_logged_simplex(self, constraints: List[Dict[str, np.ndarray]]) -> Dict[str, Any]:
        """
        执行两阶段单纯形法并记录每一步的单纯形表。

        改进内容：
        1. 实现Bland规则防止循环
        2. 修复离基变量标签获取bug
        3. 增强数值稳定性
        4. 添加基变量状态记录
        5. 添加详细中文解释
        """
        # 使用相对容差提高数值稳定性
        tolerance = 1e-9
        row_labels = [rec['label'] for rec in constraints]
        m = len(constraints)
        n = self.n_beverages

        A = np.vstack([rec['coeffs'] for rec in constraints]).astype(float)
        b = np.array([rec['rhs'] for rec in constraints], dtype=float)

        # 计算相对容差（基于RHS的量级）
        rhs_scale = max(np.max(np.abs(b)), 1.0)
        rel_tolerance = tolerance * rhs_scale

        # 决策变量：使用饮料名称更直观
        var_names = [f"x{i + 1}({self.beverage_types[i][:2]})" for i in range(n)]
        var_display_names = self.beverage_types.copy()  # 用于显示的完整名称
        var_types = ['decision'] * n
        slack_counter = 0
        surplus_counter = 0
        artificial_counter = 0

        tableau = A.copy()
        if tableau.ndim == 1:
            tableau = tableau.reshape(m, -1)
        if tableau.size == 0:
            return {'iterations': [], 'status': 'empty', 'error': '约束矩阵为空'}

        basis: List[Optional[int]] = [None] * m

        def append_column(values: np.ndarray) -> int:
            nonlocal tableau
            tableau = np.hstack((tableau, values.reshape(m, 1)))
            return tableau.shape[1] - 1

        # 构建初始单纯形表，添加松弛/剩余/人工变量
        for row_idx, constraint in enumerate(constraints):
            sense = constraint['sense']
            if sense == '<=':
                slack_counter += 1
                col_values = np.zeros(m)
                col_values[row_idx] = 1.0
                col_idx = append_column(col_values)
                var_names.append(f"s{slack_counter}")
                var_types.append('slack')
                basis[row_idx] = col_idx
            elif sense == '>=':
                # 剩余变量（系数-1）
                surplus_counter += 1
                surplus_col = np.zeros(m)
                surplus_col[row_idx] = -1.0
                append_column(surplus_col)
                var_names.append(f"e{surplus_counter}")
                var_types.append('surplus')
                # 人工变量（系数+1，作为初始基变量）
                artificial_counter += 1
                art_col = np.zeros(m)
                art_col[row_idx] = 1.0
                art_idx = append_column(art_col)
                var_names.append(f"a{artificial_counter}")
                var_types.append('artificial')
                basis[row_idx] = art_idx
            elif sense == '=':
                # 等式约束：只添加人工变量
                artificial_counter += 1
                art_col = np.zeros(m)
                art_col[row_idx] = 1.0
                art_idx = append_column(art_col)
                var_names.append(f"a{artificial_counter}")
                var_types.append('artificial')
                basis[row_idx] = art_idx
            else:
                raise ValueError(f"不支持的约束类型: {sense}")

        # 添加RHS列
        tableau = np.hstack((tableau, b.reshape(m, 1)))

        # Phase II 目标函数系数（原问题的利润最大化）
        cj_phase2 = np.zeros(len(var_names))
        for i in range(self.n_beverages):
            cj_phase2[i] = float(self.profits[i])

        # Phase I 目标函数系数（最小化人工变量之和，即最大化 -Σa）
        cj_phase1 = np.zeros(len(var_names))
        for idx, vtype in enumerate(var_types):
            if vtype == 'artificial':
                cj_phase1[idx] = -1.0

        def get_basis_var_name(row_idx: int) -> str:
            """获取指定行的基变量名称"""
            if basis[row_idx] is not None and basis[row_idx] < len(var_names):
                return var_names[basis[row_idx]]
            return f"(行{row_idx+1})"

        def compute_cj_minus_zj(cj_vec: np.ndarray) -> np.ndarray:
            """计算检验数 Cj - Zj"""
            zj = np.zeros(len(cj_vec))
            for row_idx, basic in enumerate(basis):
                if basic is None:
                    continue
                zj += cj_vec[basic] * tableau[row_idx, :-1]
            return cj_vec - zj

        def compute_objective_value(cj_vec: np.ndarray) -> float:
            """计算当前目标函数值"""
            value = 0.0
            for row_idx, basic in enumerate(basis):
                if basic is None:
                    continue
                value += cj_vec[basic] * tableau[row_idx, -1]
            return float(value)

        def get_current_solution() -> Dict[str, float]:
            """获取当前基本解"""
            solution = {}
            for row_idx, basic in enumerate(basis):
                if basic is not None and basic < len(var_names):
                    solution[var_names[basic]] = float(tableau[row_idx, -1])
            return solution

        def get_basis_info() -> List[Dict[str, Any]]:
            """获取基变量详细信息"""
            info = []
            for row_idx, basic in enumerate(basis):
                if basic is not None and basic < len(var_names):
                    info.append({
                        'row': row_idx,
                        'variable': var_names[basic],
                        'value': float(tableau[row_idx, -1]),
                        'type': var_types[basic] if basic < len(var_types) else 'unknown'
                    })
            return info

        def pivot(row_idx: int, col_idx: int) -> None:
            """执行主元消去操作"""
            pivot_value = tableau[row_idx, col_idx]
            # 数值稳定性检查
            if abs(pivot_value) < 1e-12:
                raise ValueError(f"主元值过小 ({pivot_value})，可能导致数值不稳定")
            # 主元行归一化
            tableau[row_idx, :] = tableau[row_idx, :] / pivot_value
            # 消去其他行
            for r in range(m):
                if r != row_idx:
                    factor = tableau[r, col_idx]
                    tableau[r, :] -= factor * tableau[row_idx, :]
            # 清理接近零的值，避免浮点误差累积
            tableau[np.abs(tableau) < 1e-12] = 0.0

        def select_entering_bland(cj_minus_zj: np.ndarray, valid_indices: List[int]) -> Tuple[Optional[int], Optional[float]]:
            """
            Bland规则选择入基变量：选择下标最小的正检验数变量
            防止退化情况下的循环
            """
            for idx in sorted(valid_indices):
                if cj_minus_zj[idx] > tolerance:
                    return idx, cj_minus_zj[idx]
            return None, None

        def select_leaving_bland(entering_col: np.ndarray, entering_idx: int) -> Tuple[Optional[int], Optional[float], List[Dict]]:
            """
            Bland规则选择离基变量：比值相等时选择基变量下标最小的行
            """
            ratios = []
            candidates = []

            for row_idx in range(m):
                if entering_col[row_idx] > tolerance:
                    ratio = tableau[row_idx, -1] / entering_col[row_idx]
                    ratios.append({
                        'constraint': row_labels[row_idx],
                        'ratio': float(ratio),
                        'basis_var': get_basis_var_name(row_idx)
                    })
                    candidates.append((ratio, basis[row_idx] if basis[row_idx] is not None else float('inf'), row_idx))
                else:
                    ratios.append({
                        'constraint': row_labels[row_idx],
                        'ratio': None,
                        'basis_var': get_basis_var_name(row_idx)
                    })

            if not candidates:
                return None, None, ratios

            # 先按比值排序，比值相等时按基变量下标排序（Bland规则）
            candidates.sort(key=lambda x: (x[0], x[1]))
            best_ratio, _, leave_row = candidates[0]

            return leave_row, best_ratio, ratios

        def generate_step_explanation(phase: str, iteration: int, entering: str, leaving: str,
                                      entering_value: float, best_ratio: float,
                                      is_optimal: bool = False, is_unbounded: bool = False) -> str:
            """生成详细的中文步骤解释"""
            if is_optimal:
                return f"✅ 所有检验数 Cj-Zj ≤ 0，{phase}达到最优解。"
            if is_unbounded:
                return f"⚠️ 入基列所有元素 ≤ 0，问题无界。"

            explanation = []
            explanation.append(f"📌 **{phase} 第{iteration}次迭代**")
            explanation.append(f"")
            explanation.append(f"**步骤1 - 最优性检验：**")
            explanation.append(f"  检验数 Cj-Zj 中存在正值，当前解非最优")
            explanation.append(f"")
            explanation.append(f"**步骤2 - 选择入基变量：**")
            explanation.append(f"  变量 {entering} 的检验数最大（{entering_value:.4f}）")
            explanation.append(f"  → {entering} 入基")
            explanation.append(f"")
            explanation.append(f"**步骤3 - 最小比值法选择离基变量：**")
            explanation.append(f"  最小比值 = {best_ratio:.4f}")
            explanation.append(f"  → {leaving} 离基")
            explanation.append(f"")
            explanation.append(f"**步骤4 - 主元消去：**")
            explanation.append(f"  对主元所在行进行归一化，消去其他行对应元素")

            return "\n".join(explanation)

        def run_phase(phase_name: str, cj_vec: np.ndarray, use_bland: bool = True) -> List[Dict[str, Any]]:
            """
            执行单纯形法的一个阶段

            Args:
                phase_name: 阶段名称 ('Phase I' 或 'Phase II')
                cj_vec: 目标函数系数向量
                use_bland: 是否使用Bland规则（防止循环）
            """
            history: List[Dict[str, Any]] = []
            max_iterations = 500  # 增加最大迭代次数
            iteration = 1

            while iteration <= max_iterations:
                tableau_before = tableau.copy()
                column_snapshot = var_names.copy()
                cj_minus_zj = compute_cj_minus_zj(cj_vec)
                current_obj = compute_objective_value(cj_vec)
                current_solution = get_current_solution()
                basis_info = get_basis_info()

                # 构建迭代记录
                entry: Dict[str, Any] = {
                    'phase': phase_name,
                    'iteration': iteration,
                    'status': 'pivot',
                    'column_labels': column_snapshot,
                    'row_labels': row_labels.copy(),
                    'cj_vec': cj_vec.tolist(),
                    'cj_minus_zj': cj_minus_zj.tolist(),
                    'objective_value': current_obj,
                    'tableau_before': tableau_before.tolist(),
                    'current_solution': current_solution,
                    'basis_info': basis_info,
                    'entering': None,
                    'leaving': None,
                    'pivot': None,
                    'ratios': [],
                    'explanation': ''
                }

                # 确定有效变量索引（Phase II排除人工变量）
                valid_indices = list(range(len(cj_vec)))
                if phase_name == 'Phase II':
                    valid_indices = [idx for idx in valid_indices if idx >= len(var_types) or var_types[idx] != 'artificial']

                # 选择入基变量
                if use_bland:
                    entering_idx, entering_value = select_entering_bland(cj_minus_zj, valid_indices)
                else:
                    # 最大检验数规则
                    entering_idx = None
                    entering_value = None
                    for idx in valid_indices:
                        value = cj_minus_zj[idx]
                        if entering_value is None or value > entering_value + tolerance:
                            entering_value = value
                            entering_idx = idx

                # 最优性检验
                if entering_idx is None or entering_value is None or entering_value <= tolerance:
                    entry['status'] = 'optimal'
                    entry['tableau_after'] = tableau_before.tolist()
                    entry['explanation'] = generate_step_explanation(
                        phase_name, iteration, '', '', 0, 0, is_optimal=True
                    )
                    # 提取最优解中的决策变量值
                    decision_var_values = []
                    for i in range(self.n_beverages):
                        var_name = var_names[i]
                        decision_var_values.append(current_solution.get(var_name, 0.0))
                    entry['decision_variables'] = decision_var_values
                    history.append(entry)
                    break

                # 选择离基变量（最小比值法 + Bland规则）
                entering_col = tableau[:, entering_idx]
                leave_row, best_ratio, ratios = select_leaving_bland(entering_col, entering_idx)
                entry['ratios'] = ratios

                # 无界检验
                if leave_row is None:
                    entry['status'] = 'unbounded'
                    entry['tableau_after'] = tableau_before.tolist()
                    entry['explanation'] = generate_step_explanation(
                        phase_name, iteration, '', '', 0, 0, is_unbounded=True
                    )
                    history.append(entry)
                    break

                # 记录入基和离基变量 - 修复bug：正确获取离基变量名称
                entering_label = column_snapshot[entering_idx]
                leaving_label = get_basis_var_name(leave_row)  # 修复：使用基变量名而非列索引

                entry['entering'] = entering_label
                entry['leaving'] = leaving_label
                entry['pivot'] = {
                    'row': leave_row,
                    'col': entering_idx,
                    'value': float(entering_col[leave_row]),
                    'row_label': row_labels[leave_row],
                    'col_label': entering_label
                }
                entry['explanation'] = generate_step_explanation(
                    phase_name, iteration, entering_label, leaving_label,
                    entering_value, best_ratio
                )

                # 执行主元消去
                pivot(leave_row, entering_idx)
                basis[leave_row] = entering_idx

                entry['tableau_after'] = tableau.copy().tolist()
                entry['objective_value_after'] = compute_objective_value(cj_vec)
                entry['improvement'] = entry['objective_value_after'] - current_obj
                history.append(entry)

                iteration += 1
            else:
                # 达到最大迭代次数
                history.append({
                    'phase': phase_name,
                    'iteration': max_iterations,
                    'status': 'max_iterations',
                    'column_labels': var_names.copy(),
                    'row_labels': row_labels.copy(),
                    'cj_minus_zj': compute_cj_minus_zj(cj_vec).tolist(),
                    'objective_value': compute_objective_value(cj_vec),
                    'tableau_before': tableau.copy().tolist(),
                    'tableau_after': tableau.copy().tolist(),
                    'current_solution': get_current_solution(),
                    'basis_info': get_basis_info(),
                    'entering': None,
                    'leaving': None,
                    'pivot': None,
                    'ratios': [],
                    'explanation': f'⚠️ 达到最大迭代次数 ({max_iterations})，算法未收敛'
                })
            return history

        def remove_artificials_from_basis() -> None:
            """Phase I 结束后，将人工变量换出基（如果还在基中）"""
            for row_idx, basic in enumerate(basis):
                if basic is None:
                    continue
                if basic >= len(var_types) or var_types[basic] != 'artificial':
                    continue
                # 尝试用非人工变量替换
                pivot_done = False
                for candidate_idx, vtype in enumerate(var_types):
                    if vtype == 'artificial':
                        continue
                    if abs(tableau[row_idx, candidate_idx]) > tolerance:
                        pivot(row_idx, candidate_idx)
                        basis[row_idx] = candidate_idx
                        pivot_done = True
                        break
                if not pivot_done:
                    # 该行是冗余约束，标记为None
                    basis[row_idx] = None

        def drop_artificial_columns() -> None:
            """删除人工变量列"""
            nonlocal tableau, var_names, var_types, cj_phase2
            keep_mask = [vtype != 'artificial' for vtype in var_types]
            if all(keep_mask):
                return
            # 保留非人工变量列和RHS列
            reduced = tableau[:, :-1][:, keep_mask]
            tableau = np.hstack((reduced, tableau[:, -1][:, None]))
            var_names = [name for name, keep in zip(var_names, keep_mask) if keep]
            var_types = [vtype for vtype, keep in zip(var_types, keep_mask) if keep]
            # 更新基变量索引映射
            index_mapping = {}
            new_idx = 0
            for old_idx, keep in enumerate(keep_mask):
                if keep:
                    index_mapping[old_idx] = new_idx
                    new_idx += 1
            basis[:] = [index_mapping.get(idx) if idx is not None else None for idx in basis]
            cj_phase2 = cj_phase2[keep_mask]

        # ============ 主流程 ============
        history: List[Dict[str, Any]] = []
        phase1_iterations = 0
        phase2_iterations = 0

        # 如果存在人工变量，需要执行 Phase I
        has_artificial = any(vtype == 'artificial' for vtype in var_types)

        if has_artificial:
            phase1_history = run_phase('Phase I', cj_phase1.copy())
            history.extend(phase1_history)
            phase1_iterations = len(phase1_history)

            if phase1_history:
                final_entry = phase1_history[-1]
                final_obj = final_entry['objective_value']

                # Phase I 目标值必须为0（或接近0）才表示有可行解
                if abs(final_obj) > rel_tolerance:
                    return {
                        'iterations': history,
                        'status': 'infeasible',
                        'phase1_iterations': phase1_iterations,
                        'phase2_iterations': 0,
                        'total_iterations': phase1_iterations,
                        'message': f'Phase I 目标值为 {final_obj:.6f}（非零），原问题无可行解'
                    }

            # 清理人工变量，准备 Phase II
            remove_artificials_from_basis()
            drop_artificial_columns()

        # 执行 Phase II
        phase2_history = run_phase('Phase II', cj_phase2.copy())
        history.extend(phase2_history)
        phase2_iterations = len(phase2_history)

        # 提取最终解
        final_solution = None
        optimal_value = None
        if phase2_history and phase2_history[-1]['status'] == 'optimal':
            final_entry = phase2_history[-1]
            optimal_value = final_entry.get('objective_value', 0)
            final_solution = final_entry.get('decision_variables', [])

        return {
            'iterations': history,
            'status': 'feasible' if phase2_history and phase2_history[-1]['status'] == 'optimal' else 'incomplete',
            'phase1_iterations': phase1_iterations,
            'phase2_iterations': phase2_iterations,
            'total_iterations': phase1_iterations + phase2_iterations,
            'optimal_value': optimal_value,
            'final_solution': final_solution,
            'var_names': var_names[:self.n_beverages],
            'message': '两阶段单纯形法求解完成' if final_solution else '求解未完成'
        }

    def solve_model(self):
        """
        使用单纯形法求解线性规划模型
        """
        try:
            c, A, b = self.build_matrices()
            result = linprog(c=c, A_ub=A, b_ub=b, method='highs', options={'disp': False})

            if result.success:
                ineqlin = getattr(result, 'ineqlin', None)
                shadow_prices = ineqlin.marginals if ineqlin is not None else None
                reduced_costs = getattr(result, 'reduced_costs', None)
                solution = {
                    'status': '最优解找到',
                    'optimal_value': -result.fun,
                    'decision_variables': result.x,
                    'shadow_prices': shadow_prices,
                    'reduced_costs': reduced_costs,
                    'slack_variables': result.slack,
                    'iterations': result.nit,
                    'success': True
                }
                solution['constraint_analysis'] = self.analyze_constraints(result, A, b)
                solution['simplex_iterations'] = self.generate_simplex_iterations()
                return solution

            return {
                'status': '求解失败',
                'message': result.message,
                'success': False
            }
        except Exception as exc:
            return {
                'status': '求解失败',
                'message': str(exc),
                'success': False
            }

    def analyze_constraints(self, result, A, b):
        """
        计算约束利用率、影子价格等信息
        """
        analysis = {
            'material_constraints': {},
            'transport_constraints': {},
            'binding_constraints': [],
            'non_binding_constraints': []
        }
        if not result.success:
            return analysis

        slack = result.slack if getattr(result, 'slack', None) is not None else np.zeros_like(b)
        ineqlin = getattr(result, 'ineqlin', None)
        marginals = ineqlin.marginals if ineqlin is not None else None
        tol = 1e-6
        idx = 0

        for i, material in enumerate(self.material_types):
            usage = float(np.dot(self.material_consumption[i], result.x))
            limit = float(self.material_limits[i])
            slack_val = float(slack[idx]) if slack is not None else 0.0
            shadow_price = float(marginals[idx]) if marginals is not None else 0.0
            binding = abs(usage - limit) <= tol
            analysis['material_constraints'][material] = {
                'usage': usage,
                'limit': limit,
                'slack': slack_val,
                'shadow_price': shadow_price,
                'utilization_rate': usage / limit if limit else 0.0,
                'is_binding': binding
            }
            name = f"原料-{material}"
            (analysis['binding_constraints'] if binding else analysis['non_binding_constraints']).append(name)
            idx += 1

        for j, region in enumerate(self.transport_regions):
            usage = float(np.dot(self.demand_weights[:, j], result.x))
            limit = float(self.transport_limits[j])
            slack_val = float(slack[idx]) if slack is not None else 0.0
            shadow_price = float(marginals[idx]) if marginals is not None else 0.0
            binding = abs(usage - limit) <= tol
            analysis['transport_constraints'][region] = {
                'usage': usage,
                'limit': limit,
                'slack': slack_val,
                'shadow_price': shadow_price,
                'utilization_rate': usage / limit if limit else 0.0,
                'is_binding': binding
            }
            name = f"运输-{region}"
            (analysis['binding_constraints'] if binding else analysis['non_binding_constraints']).append(name)
            idx += 1

        # 跳过最小产量与最大产量约束对应的松弛变量
        idx += self.n_beverages * 2
        return analysis

    def sensitivity_analysis(self, solution):
        """
        进行灵敏度分析，包含详细步骤记录
        """
        if not solution['success']:
            return {'error': '无法对无解模型进行灵敏度分析'}

        analysis = {
            'objective_coefficients': {},
            'rhs_changes': {},
            'recommendations': [],
            'step_logs': []
        }

        base_profits = self.profits.copy()
        base_solution = solution['decision_variables']
        tol = 1e-3
        max_iter_profit = 10
        step_counter = 1

        def log_step_entry(category: str, target: str, direction: str, tested_value: float,
                           sol_result: Optional[Dict[str, Any]] = None, status: str = 'stable',
                           note: Optional[str] = None) -> None:
            nonlocal step_counter
            feasible = bool(sol_result.get('success')) if sol_result else False
            objective_value = float(sol_result['optimal_value']) if sol_result and sol_result.get('success') else None
            snapshot = [round(float(val), 2) for val in sol_result['decision_variables']] if sol_result and sol_result.get('success') else []
            analysis['step_logs'].append({
                'step': step_counter,
                'category': category,
                'target': target,
                'direction': direction,
                'tested_value': float(tested_value),
                'status': status,
                'feasible': feasible,
                'objective_value': objective_value,
                'solution_snapshot': snapshot,
                'note': note
            })
            step_counter += 1

        def clone_base_model() -> 'BeverageOptimizationModel':
            test_model = BeverageOptimizationModel()
            test_model.profits = self.profits.copy()
            test_model.material_limits = self.material_limits.copy()
            test_model.transport_limits = self.transport_limits.copy()
            test_model.demand_weights = self.demand_weights.copy()
            test_model.previous_sales = self.previous_sales.copy()
            test_model.min_production = self.min_production.copy()
            test_model.max_production_multiplier = self.max_production_multiplier
            return test_model

        for i, beverage in enumerate(self.beverage_types):
            info = {
                'current_profit': base_profits[i],
                'optimal_production': base_solution[i],
                'reduced_cost': solution['reduced_costs'][i] if solution['reduced_costs'] is not None else 0
            }
            if solution['reduced_costs'] is not None and solution['reduced_costs'][i] > 1e-6:
                analysis['recommendations'].append(
                    f"{beverage}的当前利润过低，建议提高利润至少{solution['reduced_costs'][i]:.2f}元/升或停止生产"
                )

            orig_profit = base_profits[i]
            step_value = max(abs(orig_profit) * 0.1, 0.5)
            min_profit = orig_profit
            max_profit = orig_profit

            for k in range(1, max_iter_profit + 1):
                new_profit = orig_profit + step_value * k
                test_model = clone_base_model()
                test_model.profits = base_profits.copy()
                test_model.profits[i] = new_profit
                sol = test_model.solve_model()
                stable = sol['success'] and np.allclose(sol['decision_variables'], base_solution, atol=tol, rtol=0)
                status = 'stable' if stable else ('basis_changed' if sol['success'] else 'infeasible')
                log_step_entry('objective', beverage, 'increase', new_profit, sol, status)
                if not sol['success'] or not stable:
                    break
                max_profit = new_profit

            for k in range(1, max_iter_profit + 1):
                new_profit = orig_profit - step_value * k
                if new_profit < 0:
                    log_step_entry('objective', beverage, 'decrease', new_profit, None, 'profit_negative', '利润不能小于0')
                    break
                test_model = clone_base_model()
                test_model.profits = base_profits.copy()
                test_model.profits[i] = new_profit
                sol = test_model.solve_model()
                stable = sol['success'] and np.allclose(sol['decision_variables'], base_solution, atol=tol, rtol=0)
                status = 'stable' if stable else ('basis_changed' if sol['success'] else 'infeasible')
                log_step_entry('objective', beverage, 'decrease', new_profit, sol, status)
                if not sol['success'] or not stable:
                    break
                min_profit = new_profit

            info['range'] = (min_profit, max_profit)
            analysis['objective_coefficients'][beverage] = info

        constraint_analysis = solution['constraint_analysis']
        baseline_solution = solution['decision_variables']

        def analyse_rhs_change(target_label: str, base_value: float, apply_change) -> Tuple[float, float]:
            min_value = base_value
            max_value = base_value
            step_val = max(abs(base_value) * 0.1, 50)
            for direction in ('increase', 'decrease'):
                for k in range(1, 6):
                    delta = step_val * k
                    candidate = base_value + delta if direction == 'increase' else base_value - delta
                    if candidate <= 0 and direction == 'decrease':
                        log_step_entry('rhs', target_label, direction, candidate, None, 'limit_non_positive', '约束右侧需要为正值')
                        break
                    test_model = clone_base_model()
                    apply_change(test_model, candidate)
                    sol = test_model.solve_model()
                    stable = sol['success'] and np.allclose(sol['decision_variables'], baseline_solution, atol=1e-3, rtol=0)
                    status = 'stable' if stable else ('basis_changed' if sol['success'] else 'infeasible')
                    log_step_entry('rhs', target_label, direction, candidate, sol, status)
                    if direction == 'increase' and stable:
                        max_value = candidate
                    if direction == 'decrease' and stable:
                        min_value = candidate
                    if not sol['success'] or not stable:
                        break
            return min_value, max_value

        material_constraints = constraint_analysis.get('material_constraints', {})
        for idx, material in enumerate(self.material_types):
            if material not in material_constraints:
                continue
            info = material_constraints[material]
            if not info['is_binding']:
                continue
            entry = {
                'current_limit': info['limit'],
                'shadow_price': info['shadow_price'],
                'recommendation': f"增加{material}供应可提高利润{info['shadow_price']:.2f}元/千克"
            }

            def apply_material(model: 'BeverageOptimizationModel', value: float) -> None:
                model.material_limits[idx] = value

            entry['range'] = analyse_rhs_change(f"原料-{material}", self.material_limits[idx], apply_material)
            analysis['rhs_changes'][material] = entry

        transport_constraints = constraint_analysis.get('transport_constraints', {})
        for idx, region in enumerate(self.transport_regions):
            if region not in transport_constraints:
                continue
            info = transport_constraints[region]
            if not info['is_binding']:
                continue
            entry = {
                'current_limit': info['limit'],
                'shadow_price': info['shadow_price'],
                'recommendation': f"增加{region}运输能力可提高利润{info['shadow_price']:.2f}元/升"
            }

            def apply_transport(model: 'BeverageOptimizationModel', value: float) -> None:
                model.transport_limits[idx] = value

            entry['range'] = analyse_rhs_change(f"运输-{region}", self.transport_limits[idx], apply_transport)
            analysis['rhs_changes'][region] = entry

        return analysis

    def update_parameters(self, params: Dict):
        """
        更新模型参数
        
        Args:
            params: 参数字典
        """
        # Partial updates keep the solver responsive to UI slider changes
        if 'profits' in params:
            self.profits = np.array(params['profits'])
        
        if 'material_limits' in params:
            self.material_limits = np.array(params['material_limits'])
        
        if 'transport_limits' in params:
            self.transport_limits = np.array(params['transport_limits'])
        
        if 'min_production_ratio' in params:
            ratio = params['min_production_ratio']
            self.min_production = ratio * self.previous_sales
        
        if 'max_production_multiplier' in params:
            self.max_production_multiplier = params['max_production_multiplier']

# 创建全局模型实例
model = BeverageOptimizationModel()
