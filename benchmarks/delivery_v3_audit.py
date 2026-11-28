"""Run with python -m benchmarks.delivery_v3_audit. Uses isolated Modal resources."""
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import modal
from PIL import Image

from runtime.config.storage import volume_key
from runtime.delivery import outbox, v3
from tests.test_durable_delivery import Ledger, fixture, make_receipt


def main():
    with modal.Dict.ephemeral() as registry:
        for stage in ("original", "derivative"):
            barrier = Barrier(8)

            def compete(_):
                barrier.wait()
                return v3.reserve_worker(registry, stage, "d" * 64)

            with ThreadPoolExecutor(8) as pool:
                winners = sum(pool.map(compete, range(8)))
            assert winners == 1, (stage, winners)
            print(stage, "concurrent owners", winners, flush=True)

    with (modal.Volume.ephemeral(version=2) as source, modal.Volume.ephemeral(version=2) as metadata,
          modal.Dict.ephemeral() as registry):
        os.environ.update(DELIVERY_V3_APP_NAME="lab-delivery-v3-audit-" + uuid.uuid4().hex[:8],
                          DELIVERY_V3_SCHEDULE="0", DELIVERY_V3_LAB_SOURCE_ID=source.object_id,
                          DELIVERY_V3_LAB_LEDGER_ID=metadata.object_id, DELIVERY_V3_LAB_REGISTRY_ID=registry.object_id)
        from pipeline import deploy_delivery_v3 as deployment

        good = io.BytesIO()
        Image.new("RGBA", (16, 16)).save(good, format="PNG")
        ledger = v3.Ledger(metadata)
        ids = []
        # The damaged file passes the archive's signature check but cannot decode.
        for n, data in enumerate((good.getvalue(), b"\x89PNG\r\n\x1a\n" + b"broken" * 10)):
            local = Ledger()
            intent, _ = fixture(local, n, raw=data)
            req = outbox.resolve(local, intent)
            with source.batch_upload() as batch:
                batch.put_file(io.BytesIO(data), volume_key("/" + req["artifact"]["path"]))
            v3.register(ledger, intent)
            v3.save_receipts(ledger, [(intent, req, make_receipt(req))])
            ids.append(intent["id"])
        with deployment.app.run():
            result = deployment.derivative_worker.remote(ids)
            repeated = deployment.derivative_worker.remote(ids)
            rows = [outbox.read(ledger, outbox.path(i, "derivative.json")) for i in ids]
            assert [r and r["status"] for r in rows] == ["original", "failed"], rows
            assert result["processed"] == 2 and repeated["processed"] == 0
            assert not list(v3.active_ids(ledger, True))
            print("remote receipts", rows, "repeat", repeated, flush=True)


if __name__ == "__main__":
    main()
