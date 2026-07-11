"""Tests for gzip-compressed webhook pushes (LTE data-saver support).

aiohttp inflates ``Content-Encoding: gzip`` request bodies transparently,
but only bounds the *compressed* size; the handler's bounded read is what
stops decompression bombs. These tests pin down the full behavior matrix:
valid gzip, garbage, truncation, bombs, and plain JSON.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.wican.const import MAX_WEBHOOK_BODY_BYTES
from tests.device_sim import MeatPiDeviceSimulator

GZIP_HEADERS = {"Content-Encoding": "gzip"}


async def _sim(hass: HomeAssistant, hass_client: Any) -> MeatPiDeviceSimulator:
    sim = MeatPiDeviceSimulator(hass, hass_client)
    await sim.async_setup()
    return sim


async def _assert_alive(hass: HomeAssistant, sim: MeatPiDeviceSimulator) -> None:
    """The telemetry pipeline still works (fresh client after aborts)."""
    sim._client = None
    resp = await sim.push_and_settle(sim.status(batt_voltage="12.2V"))
    assert resp.status == 204
    assert float(hass.states.get("sensor.wican_sim_battery_voltage").state) == 12.2


async def test_gzip_push_round_trip(hass: HomeAssistant, hass_client: Any) -> None:
    """A gzip-compressed push updates entities exactly like a plain one."""
    sim = await _sim(hass, hass_client)
    payload = {**sim.status(batt_voltage="12.9V"), **sim.pids({"SOC": 55})}
    body = gzip.compress(json.dumps(payload).encode())

    resp = await sim.push(raw=body, headers=GZIP_HEADERS)
    await hass.async_block_till_done()

    assert resp.status == 204
    assert float(hass.states.get("sensor.wican_sim_battery_voltage").state) == 12.9
    assert hass.states.get("sensor.wican_sim_soc") is not None
    # Compression is worthwhile: the wire body is smaller than the JSON.
    assert len(body) < len(json.dumps(payload))


async def test_garbage_gzip_rejected_cleanly(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A body claiming gzip but containing garbage cannot wedge anything."""
    sim = await _sim(hass, hass_client)

    resp = await sim.push(raw=b"definitely not gzip", headers=GZIP_HEADERS)
    # The broken stream is rejected (422 from the handler, or 200 when
    # Home Assistant's webhook wrapper contains the transport error first).
    assert resp.status in (200, 422)

    await _assert_alive(hass, sim)


async def test_truncated_gzip_contained(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A truncated gzip stream aborts that request only."""
    sim = await _sim(hass, hass_client)
    body = gzip.compress(json.dumps(sim.status()).encode())[:20]

    try:
        resp = await sim.push(raw=body, headers=GZIP_HEADERS)
        assert resp.status in (200, 400, 422)
    except OSError:
        # aiohttp may abort the connection at the protocol level — that is
        # an acceptable containment too (a real device would retry).
        pass

    await _assert_alive(hass, sim)


async def test_decompression_bomb_bounded(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """A compression bomb is refused without being fully inflated.

    aiohttp's payload layer aborts the suspicious stream mid-read
    (RequestPayloadError) and the handler answers a clean 422; the
    handler's own read cap remains as defense-in-depth for streams the
    payload layer does not pre-guard.
    """
    sim = await _sim(hass, hass_client)
    # 64 MiB of zeros compresses to ~64 KiB — 32× past the decompressed cap.
    bomb = gzip.compress(b"0" * (MAX_WEBHOOK_BODY_BYTES * 32))
    assert len(bomb) < 1024 * 1024  # sanity: the wire payload is small

    try:
        resp = await sim.push(raw=bomb, headers=GZIP_HEADERS)
        assert resp.status in (413, 422)
    except OSError:
        pass

    await _assert_alive(hass, sim)


async def test_oversized_plain_body_rejected(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """An uncompressed body past the cap gets 413 from the read bound."""
    sim = await _sim(hass, hass_client)
    huge = b'{"status": {"pad": "' + b"x" * (MAX_WEBHOOK_BODY_BYTES + 1024) + b'"}}'

    resp = await sim.push(raw=huge)
    assert resp.status == 413

    await _assert_alive(hass, sim)


async def test_plain_pushes_unaffected(hass: HomeAssistant, hass_client: Any) -> None:
    """Uncompressed pushes (all current firmware) behave exactly as before."""
    sim = await _sim(hass, hass_client)

    resp = await sim.push_and_settle(
        {**sim.status(batt_voltage="12.7V"), **sim.pids({"RPM": 900})},
    )

    assert resp.status == 204
    assert float(hass.states.get("sensor.wican_sim_battery_voltage").state) == 12.7
    assert hass.states.get("sensor.wican_sim_rpm") is not None


async def test_invalid_plain_json_still_422(
    hass: HomeAssistant, hass_client: Any,
) -> None:
    """The existing invalid-JSON contract is unchanged."""
    sim = await _sim(hass, hass_client)

    resp = await sim.push(raw=b"not json at all")
    assert resp.status == 422

    await _assert_alive(hass, sim)
