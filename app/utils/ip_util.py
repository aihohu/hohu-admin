"""客户端 IP 解析工具

反向代理（nginx / Docker / 云负载均衡）场景下，request.client.host 取到的是上游容器/代理 IP，
真实客户端 IP 需要从 X-Forwarded-For 或 X-Real-IP 头读取。
"""

from fastapi import Request


def get_client_ip(request: Request) -> str | None:
    """获取真实客户端 IP。

    解析顺序：
    1. X-Forwarded-For 的第一个值（原始客户端；nginx 通过 $proxy_add_x_forwarded_for 自动追加）
    2. X-Real-IP（部分代理只设置这个头）
    3. request.client.host（直连场景下的对端 IP）

    注意：X-Forwarded-For 可被客户端伪造。当前实现信任第一段，适用于「后端不直接暴露公网」的场景。
    若后端将直接暴露公网，需引入可信代理白名单逻辑。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # 格式：client, proxy1, proxy2 — 取第一个并去空白
        first = xff.split(",")[0].strip()
        if first:
            return first

    if x_real := request.headers.get("x-real-ip"):
        return x_real.strip()

    return request.client.host if request.client else None
