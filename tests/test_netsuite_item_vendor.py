"""Tests for the item-master vendor price helpers.

``ensure_item_vendor_price`` is compare-first: it only rewrites the item's
``itemVendor`` sublist when the target vendor is not already preferred at the
requested price. All NetSuite calls are mocked.
"""

from unittest.mock import MagicMock

from includes.netsuite.records.item import ensure_item_vendor_price


def _client(item_vendor_items):
    client = MagicMock()
    client.get.return_value.json.return_value = {
        "itemVendor": {"items": item_vendor_items}
    }
    return client


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        "includes.netsuite.records.item.NetSuiteClient", lambda: client
    )


class TestEnsureItemVendorPrice:
    def test_unchanged_preferred_vendor_skips_write(self, monkeypatch):
        client = _client([
            {"vendor": {"id": "77"}, "preferredVendor": True, "purchasePrice": 10.0},
        ])
        _patch_client(monkeypatch, client)

        result = ensure_item_vendor_price("555", "77", 10.0)

        assert result.success
        assert result.netsuite_id == "555"
        client.get.assert_called_once()
        client.update_record.assert_not_called()

    def test_rounding_difference_skips_write(self, monkeypatch):
        client = _client([
            {"vendor": {"id": "77"}, "preferredVendor": True, "purchasePrice": 10.0},
        ])
        _patch_client(monkeypatch, client)

        result = ensure_item_vendor_price("555", "77", 10.001)

        assert result.success
        client.update_record.assert_not_called()

    def test_changed_price_rewrites_and_preserves_other_vendors(self, monkeypatch):
        client = _client([
            {"vendor": {"id": "77"}, "preferredVendor": True, "purchasePrice": 10.0},
            {"vendor": {"id": "88"}, "preferredVendor": False, "purchasePrice": 3.0},
        ])
        _patch_client(monkeypatch, client)

        result = ensure_item_vendor_price("555", "77", 12.5)

        assert result.success
        # The compare step's read is reused by the writer — one GET, not two.
        client.get.assert_called_once()
        assert client.update_record.call_count == 2
        clear_call, add_call = client.update_record.call_args_list
        assert clear_call.args[:2] == ("inventoryitem", "555")
        assert clear_call.args[2] == {"itemVendor": {"items": []}}
        assert clear_call.kwargs["params"] == {"replace": "itemVendor"}
        rebuilt = add_call.args[2]["itemVendor"]["items"]
        assert {"vendor": {"id": "77"}, "preferredVendor": True,
                "purchasePrice": 12.5} in rebuilt
        assert {"vendor": {"id": "88"}, "preferredVendor": False,
                "purchasePrice": 3.0} in rebuilt

    def test_vendor_not_preferred_triggers_write(self, monkeypatch):
        client = _client([
            {"vendor": {"id": "77"}, "preferredVendor": False, "purchasePrice": 10.0},
        ])
        _patch_client(monkeypatch, client)

        result = ensure_item_vendor_price("555", "77", 10.0)

        assert result.success
        client.get.assert_called_once()
        assert client.update_record.call_count == 2

    def test_read_failure_returns_error(self, monkeypatch):
        client = MagicMock()
        client.get.side_effect = RuntimeError("boom")
        _patch_client(monkeypatch, client)

        result = ensure_item_vendor_price("555", "77", 10.0)

        assert not result.success
        assert "boom" in (result.error or "")
        client.update_record.assert_not_called()
