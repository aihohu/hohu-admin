"""审计中间件测试共用 fixture。

测试用 mock 替代 redis_client 和 AsyncSessionLocal，所以这里不需要
db_session fixture 也不需要 redis reset。保留 conftest 占位以便未来
扩展端到端集成测试。
"""
