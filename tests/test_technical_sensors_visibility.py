"""Technical statistics sensors stay functional but hidden from device cards."""


def test_technical_statistics_sensors_hidden_by_default():
    """The recorder-feeding sensors must not clutter device cards.

    They still work in the background (LTS / Energy Dashboard); the registry
    default only hides them in the UI, avoiding a confusing "unknown" state.
    """
    from custom_components.energa_mobile.sensors.live import (
        EnergaCostStatisticsSensor,
        EnergaStatisticsSensor,
    )

    assert EnergaStatisticsSensor._attr_entity_registry_visible_default is False
    assert EnergaCostStatisticsSensor._attr_entity_registry_visible_default is False
