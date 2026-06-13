"""
频率限制中间件

使用内存存储实现简单的频率限制功能，
防止暴力破解和恶意请求。
"""

from datetime import datetime, timedelta

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.utils.ip_util import get_client_ip


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    频率限制中间件

    使用内存存储实现简单的频率限制，
    基于 IP 地址和端点路径进行限制。
    """

    def __init__(self, app):
        super().__init__(app)
        # 存储每个 IP 和路径的请求记录
        # 格式：{ip: {path: [(timestamp, count)]}}
        self.requests = {}

    async def dispatch(self, request: Request, call_next):
        """
        处理请求并进行频率限制检查

        Args:
            request: FastAPI 请求对象
            call_next: 下一个中间件或路由处理器

        Returns:
            Response: 处理后的响应对象
        """
        # 获取客户端 IP
        client_ip = get_client_ip(request) or "unknown"

        # 获取请求路径
        path = request.url.path

        # 检查是否需要频率限制
        rate_limit = self._get_rate_limit(path)
        if not rate_limit:
            # 不需要限制，直接放行
            return await call_next(request)

        # SSE 流式端点跳过 BaseHTTPMiddleware，避免流式响应中断
        if path == "/ai/chat":
            return await call_next(request)

        # 检查频率限制
        now = datetime.now()
        if self._is_rate_limited(client_ip, path, now, rate_limit):
            # 超出限制，返回 429
            return JSONResponse(
                status_code=429,
                content={"code": 429, "msg": "请求过于频繁，请稍后再试", "data": None},
            )

        # 清理过期记录
        self._cleanup_expired_records(now)

        # 记录本次请求
        self._record_request(client_ip, path, now)

        # 放行请求
        return await call_next(request)

    def _get_rate_limit(self, path: str) -> tuple[int, int]:
        """
        根据路径获取频率限制配置

        Args:
            path: 请求路径

        Returns:
            tuple[int, int]: (max_requests, time_window_seconds)
        """
        # 登录接口
        if "/login" in path:
            return 5, 60  # 每分钟最多 5 次
        # 注册接口
        if "/register" in path:
            return 3, 60  # 每分钟最多 3 次
        # 其他接口使用通用限制
        return 100, 60  # 每分钟最多 100 次

    def _is_rate_limited(
        self, ip: str, path: str, now: datetime, rate_limit: tuple[int, int]
    ) -> bool:
        """
        检查是否超出频率限制

        Args:
            ip: 客户端 IP 地址
            path: 请求路径
            now: 当前时间
            rate_limit: 频率限制配置 (max_requests, time_window_seconds)

        Returns:
            bool: 是否超出限制
        """
        max_requests, time_window = rate_limit

        if ip not in self.requests:
            return False

        if path not in self.requests[ip]:
            return False

        # 获取该 IP 和路径的请求记录
        records = self.requests[ip][path]

        # 清理过期的记录
        cutoff_time = now - timedelta(seconds=time_window)
        records = [r for r in records if r[0] >= cutoff_time]

        # 计算时间窗口内的请求数
        total_requests = sum(r[1] for r in records)

        return total_requests >= max_requests

    def _record_request(self, ip: str, path: str, now: datetime):
        """
        记录请求

        Args:
            ip: 客户端 IP 地址
            path: 请求路径
            now: 当前时间
        """
        if ip not in self.requests:
            self.requests[ip] = {}

        if path not in self.requests[ip]:
            self.requests[ip][path] = []

        self.requests[ip][path].append((now, 1))

    def _cleanup_expired_records(self, now: datetime):
        """
        清理过期的请求记录

        Args:
            now: 当前时间
        """
        # 清理超过 1 小时的记录
        cutoff_time = now - timedelta(hours=1)

        for ip in list(self.requests.keys()):
            for path in list(self.requests[ip].keys()):
                self.requests[ip][path] = [
                    r for r in self.requests[ip][path] if r[0] >= cutoff_time
                ]
                # 删除空列表
                if not self.requests[ip][path]:
                    del self.requests[ip][path]
