"""自适应并发控制 + 熔断器 + 超时工具"""
import asyncio
import time
from collections import deque
from contextvars import ContextVar

#: 当前 AI 调用所属的后台任务。ContextVar 会随 asyncio Task 上下文自动复制，
#: 避免用全局变量把并发方案的 task_id 串台。默认空串保持旧调用行为。
current_ai_task_id: ContextVar[str] = ContextVar(
    "current_ai_task_id", default=""
)


class ResizableSemaphore:
    """可动态调整容量的信号量。

    原生 ``asyncio.Semaphore`` 一旦重建对象，旧对象上正在等待许可的协程会
    永久拿不到许可而饿死。正文/目录生成在自适应调整并发时会触发该问题
    （审查结论 A1）。本类 ``set_value`` 仅改容量、不丢弃等待者。
    """

    def __init__(self, value: int):
        self._capacity = value
        self._held = 0
        self._value = value
        # 等待者队列（FIFO）。每项为 (Future, owner_id)，owner_id 来自
        # current_ai_task_id，用于暂停时只拒绝当前任务的排队请求。
        self._waiters: "deque[tuple[asyncio.Future, str]]" = deque()

    def _wake(self, count: int):
        """唤醒最多 count 个等待者（跳过已完成/已取消的）。"""
        woken = 0
        while self._waiters and woken < count:
            fut, _owner = self._waiters.popleft()
            if fut.done():
                continue
            fut.set_result(True)
            woken += 1

    def reject_waiters(self, owner_id: str | None = None) -> int:
        """拒绝排队等待许可的协程，可按后台任务归属隔离。

        ``owner_id`` 为空时保持旧行为（拒绝全部）；指定任务时仅拒绝该任务
        的 waiter，避免暂停方案 A 误取消方案 B 的 AI 请求。
        """
        rejected = 0
        kept: deque[tuple[asyncio.Future, str]] = deque()
        while self._waiters:
            fut, owner = self._waiters.popleft()
            if fut.done():
                continue
            if owner_id and owner and owner != owner_id:
                kept.append((fut, owner))
                continue
            fut.set_result(False)
            rejected += 1
        self._waiters.extend(kept)
        return rejected

    async def acquire(self, owner_id: str | None = None):
        owner = current_ai_task_id.get() if owner_id is None else owner_id
        while self._value <= 0:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append((fut, owner))
            try:
                # 被唤醒后重新检查容量：期间许可可能已被其他协程抢走
                result = await fut
                if result is False:
                    # 被 reject_waiters 拒绝：立即返回 False，上层据此放弃排队
                    return False
            except BaseException:
                if not fut.done():
                    fut.cancel()
                try:
                    self._waiters.remove((fut, owner))
                except ValueError:
                    pass
                raise
        self._value -= 1
        self._held += 1
        return True

    def release(self):
        self._held -= 1
        self._value += 1
        self._wake(1)

    def set_value(self, value: int):
        """只调整容量，不重建对象，已在等待/已持有许可的协程不受影响。

        ✅ 修复（2026-09-17）：旧实现直接 ``self._value = value`` 覆盖可用许可数，
        但在其它协程仍持有许可时调用（并发控制器在持锁中调整），会把已持有数
        再次叠加进总容量，导致有效并发永久膨胀（恰恰在 429 风暴等最需要降并发的
        场景放大并发，与「并发夹在 [target-2, target]」目标相反）。
        现记录容量、可用许可 = max(0, 容量 - 已持有)，上调时唤醒等待者重新竞争。
        """
        self._capacity = value
        self._value = max(0, self._capacity - self._held)
        if self._value > 0:
            # 容量上调：唤醒全部等待者重新竞争，避免"许可变多但没人被叫醒"
            self._wake(len(self._waiters))

    async def __aenter__(self):
        ok = await self.acquire()
        if not ok:
            # 被拒绝：抛 CancelledError 让上层 asyncio 取消分支捕获
            raise asyncio.CancelledError("Semaphore acquire rejected (scope paused)")
        return self

    async def __aexit__(self, *exc):
        self.release()


class AnalysisCircuitBreaker:
    """AI 熔断器：窗口内连续失败≥N次 → OPEN，冷却后 → HALF_OPEN，探测成功 → CLOSED。

    改为 **per-provider 隔离**——每个 provider 维护自己的失败计数，
    避免某个 provider（如 sensetime）的连续失败把所有其他 provider 一起熔断。

    ✅ BUG-5 修复：429 限流错误采用加倍冷却（最多 120 秒），
    避免 sensetime 等限流窗口未过就 HALF_OPEN 又被 429 打回来。

    ✅ 2026-09-16 修复（P0 级）：failure_threshold 从 2 提高到 5，并加入
    **时间窗口防抖**（10 秒内的失败才计入同一窗口）。根因：主 provider 因
    2 次瞬时抖动（并发请求网络闪断/超时）就被熔断，后续几百个请求全部落到
    成功率仅 59% 的 sensetime 等 fallback 上 → 整批 55 章全部失败。
    """

    # ✅ 时间窗口：只有窗口内的连续失败才触发熔断（秒）
    FAILURE_WINDOW = 10.0

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: int = 15):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds  # 基础冷却，429 时加倍
        # provider_name → {
        #   "state": str, "failures": int, "opened_at": float,
        #   "effective_cooldown": int, "last_failure_at": float
        # }
        self._per_provider: dict[str, dict] = {}

    def _get_or_init(self, provider_name: str) -> dict:
        entry = self._per_provider.get(provider_name)
        if entry is None:
            entry = {"state": "CLOSED", "failures": 0, "opened_at": 0.0,
                     "effective_cooldown": self.cooldown_seconds,
                     "last_failure_at": 0.0}
            self._per_provider[provider_name] = entry
        return entry

    def allow_request(self, provider_name: str = "") -> bool:
        """判断某个 provider 当前是否允许请求。"""
        if not provider_name:
            return any(e["state"] != "OPEN" for e in self._per_provider.values()) or not self._per_provider
        entry = self._get_or_init(provider_name)
        if entry["state"] == "CLOSED":
            return True
        if entry["state"] == "OPEN":
            if time.time() - entry["opened_at"] >= entry["effective_cooldown"]:
                entry["state"] = "HALF_OPEN"
                return True
            return False
        # HALF_OPEN 放行单个探测
        return True

    def cooldown_remaining(self, provider_name: str) -> float:
        """返回该 provider 熔断冷却的剩余秒数（未熔断/冷却已过返回 0）。"""
        entry = self._per_provider.get(provider_name)
        if not entry or entry["state"] != "OPEN":
            return 0.0
        return max(0.0, entry["effective_cooldown"] - (time.time() - entry["opened_at"]))

    def force_probe(self, provider_name: str) -> None:
        """✅ 强制放行一次探测（全部候选都被熔断时使用）。

        背景：降级候选被收敛到 ``ai_fallback_chain_max``（默认 3）个后，
        实测出现「3 个候选全部 OPEN」的窗口 —— 旧实现此时直接抛错，
        一次真实请求都不发（运行库里 3180 条 circuit_skipped 全是这种空转）。
        熔断的本意是「少打」，不是「一个都不打」：这里把冷却剩余最短的候选
        置为 HALF_OPEN，让它承担一次探测，成功即复位，失败也只是回到 OPEN。
        """
        entry = self._get_or_init(provider_name)
        if entry["state"] == "OPEN":
            entry["state"] = "HALF_OPEN"

    def record_success(self, provider_name: str = ""):
        if provider_name:
            entry = self._get_or_init(provider_name)
            entry["state"] = "CLOSED"
            entry["failures"] = 0
            entry["effective_cooldown"] = self.cooldown_seconds  # 成功后重置冷却
            entry["last_failure_at"] = 0.0
        else:
            for e in self._per_provider.values():
                e["state"] = "CLOSED"
                e["failures"] = 0
                e["effective_cooldown"] = self.cooldown_seconds
                e["last_failure_at"] = 0.0

    def record_failure(self, provider_name: str = "", *, is_429: bool = False):
        """记录失败。

        Args:
            provider_name: provider 名
            is_429: 是否是 429 限流（加倍冷却，最多 120 秒）
        """
        now = time.time()
        if provider_name:
            entry = self._get_or_init(provider_name)
            # ✅ 时间窗口防抖：如果上次失败已超过窗口时间，重置计数
            if entry["last_failure_at"] and (now - entry["last_failure_at"]) > self.FAILURE_WINDOW:
                entry["failures"] = 0
            entry["failures"] += 1
            entry["last_failure_at"] = now
            # ✅ BUG-5: 429 加倍冷却，最多 120 秒
            if is_429:
                entry["effective_cooldown"] = min(
                    entry["effective_cooldown"] * 2, 120)
            if entry["state"] == "HALF_OPEN" or entry["failures"] >= self.failure_threshold:
                entry["state"] = "OPEN"
                entry["opened_at"] = now
        else:
            for e in self._per_provider.values():
                e["failures"] += 1
                e["state"] = "OPEN"
                e["opened_at"] = now


class AdaptiveConcurrencyController:
    """根据 AI 失败率 / 限流动态调整并发数（滑动窗口 20）。

    ✅ 2026-09-17 修正（性能瓶颈 P0-2 · 反向棘轮）：
      旧实现「平均响应 > SLOW_THRESHOLD(30s) → 并发 -1」是**方向性错误** ——
      响应慢来自服务端推理速度，降低本地并发不会让它变快，只会等比拉长总耗时；
      而回弹条件「平均响应 < FAST_THRESHOLD(8s)」在慢模型下**永远不可能满足**，
      于是并发被一路降到 min_c=1 并永久钉死。实测某次 269 次调用的批量生成
      平均并发仅 1.5（档位 4），而同规模、并发 4.1 的会话墙钟只有它的 1/5。

      现规则：
      · **只在 429 限流或失败率过高时降并发**（那才是"本地压力过大"的信号）；
      · **单纯的响应慢不降并发**（保持吞吐，让总耗时随并发下降）；
      · 快速 + 低失败率时升并发，且**围绕目标档位 target 波动**
        （上限 = target，下限 = max(min_c, target - MAX_DOWNGRADE_FROM_TARGET)），
        杜绝单向棘轮。
    """

    FAST_THRESHOLD = 8.0
    SLOW_THRESHOLD = 30.0
    WINDOW_SIZE = 20
    RATE_LIMIT_FAST_DOWNGRADE_THRESHOLD = 2
    # ✅ 降并发仅由「失败率」触发；健康线用于升并发
    FAILURE_RATE_DOWNGRADE_THRESHOLD = 0.3
    FAILURE_RATE_HEALTHY_THRESHOLD = 0.05
    # 相对目标档位最多下调的档数（防止一次抖动把并发打到 1）
    MAX_DOWNGRADE_FROM_TARGET = 2

    def __init__(self, initial: int = 3, min_c: int = 1, max_c: int = 5):
        self._sem = ResizableSemaphore(initial)
        self.current = initial
        self.target = initial      # 目标档位（配置值 / 用户档位），自适应围绕它波动
        self.min_c = min_c
        self.max_c = max_c
        self._window: deque = deque(maxlen=self.WINDOW_SIZE)
        self._consecutive_429 = 0

    @property
    def semaphore(self) -> ResizableSemaphore:
        return self._sem

    def set_concurrency(self, value: int):
        """显式设置并发数（同时刷新自适应波动的**目标档位** target）。

        仅调整信号量容量（``set_value``），**绝不重建信号量对象**——
        否则旧对象上正在等待许可的协程永久拿不到许可，导致正文/目录
        并发生成在自适应调整下饿死（审查结论 A1）。
        """
        value = max(self.min_c, min(self.max_c, int(value)))
        self.target = value
        if value == self.current:
            return
        self.current = value
        self._sem.set_value(value)

    def record(self, duration: float, status: int = 200):
        self._window.append((duration, status))
        if status == 429:
            self._consecutive_429 += 1
        elif status < 400:
            self._consecutive_429 = 0

    def failure_rate(self) -> float:
        if not self._window:
            return 0.0
        return sum(1 for _, s in self._window if s >= 400) / len(self._window)

    def avg_response(self) -> float:
        if not self._window:
            return 0.0
        return sum(d for d, _ in self._window) / len(self._window)

    def adjust_concurrency(self):
        """按失败率 / 限流调整并发（不再因"响应慢"降并发，见类 docstring）。"""
        if len(self._window) < self.WINDOW_SIZE:
            return
        fr = self.failure_rate()
        # ① 429 限流：确定信号 → 立即降并发并清零计数（本次只降一档）
        if self._consecutive_429 >= self.RATE_LIMIT_FAST_DOWNGRADE_THRESHOLD:
            self._change(-1)
            self._consecutive_429 = 0
            return
        # ② 失败率过高：降并发
        if fr > self.FAILURE_RATE_DOWNGRADE_THRESHOLD:
            self._change(-1)
            return
        # ③ 响应慢但成功率高 → **保持不变**（旧实现在此处降并发，是本次修正的核心）
        # ④ 快且健康 → 升并发（上限为目标档位 target）
        if (self.avg_response() < self.FAST_THRESHOLD
                and fr < self.FAILURE_RATE_HEALTHY_THRESHOLD):
            self._change(+1)

    def _change(self, delta: int):
        """调整并发，并夹在 [max(min_c, target - N), target] 区间内（防单向棘轮）。"""
        lo = max(self.min_c, self.target - self.MAX_DOWNGRADE_FROM_TARGET)
        hi = max(lo, min(self.max_c, self.target))
        new_c = max(lo, min(hi, self.current + delta))
        if new_c == self.current:
            return
        self.current = new_c
        self._sem.set_value(new_c)


# 全局实例（正文/目录生成共用）
concurrency_controller = AdaptiveConcurrencyController()
circuit_breaker = AnalysisCircuitBreaker()
