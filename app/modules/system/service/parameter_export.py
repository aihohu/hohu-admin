"""Encode an already authorized detached snapshot outside every DB scope."""

from io import BytesIO

from openpyxl import Workbook

from app.core.authorization_io import BoundedWorker

_worker = BoundedWorker(workers=4, queue=32, timeout=3.0)


def _encode(snapshot):
    book = Workbook()
    sheet = book.active
    try:
        sheet.title = "Configuration"
        sheet.append(snapshot["headers"])
        for values in snapshot["records"]:
            sheet.append([values.get(name) for name in snapshot["headers"]])
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        buffer = BytesIO()
        book.save(buffer)
        buffer.seek(0)
        return buffer
    finally:
        book.close()


async def encode_parameter_export(snapshot):
    return await _worker.run(_encode, snapshot)
