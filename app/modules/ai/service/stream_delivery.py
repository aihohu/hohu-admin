"""Run-wide bounded send checks; no transaction spans a provider/client await."""

from app.core.exceptions import AuthorizationException


def stopped():
    return AuthorizationException(
        "当前来源或交付预算已变化，请重新查询", error_code="FIELD_AUTHORIZATION_CHANGED"
    )


class StreamDelivery:
    def __init__(self, check, probe):
        self.check, self.probe = check, probe
        self.authority = None
        self.checks = self.bytes = self.statements = self.decoded_bytes = (
            self.renewals
        ) = 0

    def charge(self, result):
        self.statements += result["statements"]
        self.decoded_bytes += result["decoded_bytes"]
        if self.statements > 4096 or self.decoded_bytes > 128 * 1024 * 1024:
            raise stopped()

    async def model(self):
        result = await self.check()
        self.charge(result)
        self.authority = result["authority"]
        return self.authority

    async def send(self, chunk, *, force_full=False):
        self.checks += 1
        self.bytes += len(chunk.encode("utf-8"))
        if self.checks > 512 or self.bytes > 256 * 1024:
            raise stopped()
        if self.authority is None or force_full:
            await self.model()
        while True:
            result = await self.probe(self.authority)
            self.charge(result)
            if result["unchanged"]:
                return
            if self.renewals >= 2:
                raise stopped()
            self.renewals += 1
            await self.model()

    @property
    def fixed_receipt(self):
        return bool(
            self.authority
            and any(
                g.get("kind") == "completion"
                for g in self.authority.get("source_domains", ())
            )
        )
