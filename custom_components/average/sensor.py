#  Copyright (c) 2019-2020, Andrey "Limych" Khrolenok <andrey@khrolenok.ru>
#  Creative Commons BY-NC-SA 4.0 International Public License
#  (see LICENSE.md or https://creativecommons.org/licenses/by-nc-sa/4.0/)

"""
The Average Sensor.

For more details about this sensor, please refer to the documentation at
https://github.com/Limych/ha-average/
"""
import datetime
import logging
import math
import numbers
from typing import Union, Optional, Dict, Any

import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components import history
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.components.group import expand_entity_ids
from homeassistant.components.history import LazyState
from homeassistant.components.min_max.sensor import ATTR_MEAN, CONF_ROUND_DIGITS
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN
from homeassistant.const import (
    CONF_NAME,
    CONF_ENTITIES,
    EVENT_HOMEASSISTANT_START,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
    ATTR_ICON,
    CONF_TYPE,
)
from homeassistant.core import callback, split_entity_id
from homeassistant.exceptions import TemplateError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change
from homeassistant.util import Throttle
from homeassistant.util.temperature import convert as convert_temperature
from homeassistant.util.unit_system import TEMPERATURE_UNITS

from .const import (
    VERSION,
    ISSUE_URL,
    CONF_PERIOD_KEYS,
    CONF_DURATION,
    CONF_START,
    CONF_END,
    CONF_PRECISION,
    ATTR_TO_PROPERTY,
    UPDATE_MIN_TIME,
    CONF_PROCESS_UNDEF_AS,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)


def check_period_keys(conf):
    """Ensure maximum 2 of CONF_PERIOD_KEYS are provided."""
    count = sum(param in conf for param in CONF_PERIOD_KEYS)
    if (count == 1 and CONF_DURATION not in conf) or count > 2:
        raise vol.Invalid(
            "You must provide none, only "
            + CONF_DURATION
            + " or maximum 2 of the following: "
            ", ".join(CONF_PERIOD_KEYS)
        )
    return conf


PLATFORM_SCHEMA = vol.All(
    PLATFORM_SCHEMA.extend(
        {
            vol.Optional(CONF_TYPE, default=SENSOR_TYPES[ATTR_MEAN]): vol.All(
                cv.string, vol.In(SENSOR_TYPES.values())
            ),
            vol.Required(CONF_ENTITIES): cv.entity_ids,
            vol.Optional(CONF_NAME): cv.string,
            vol.Optional(CONF_START): cv.template,
            vol.Optional(CONF_END): cv.template,
            vol.Optional(CONF_DURATION): cv.time_period,
            vol.Optional(CONF_ROUND_DIGITS, default=2): int,
            vol.Optional(CONF_PRECISION, default=2): int,
            vol.Optional(CONF_PROCESS_UNDEF_AS): float,
        }
    ),
    check_period_keys,
)


# pylint: disable=unused-argument
async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up platform."""
    # Print startup message
    _LOGGER.info("Version %s", VERSION)
    _LOGGER.info(
        "If you have ANY issues with this, please report them here: %s", ISSUE_URL
    )

    sensor_type = config.get(CONF_TYPE)
    name = config.get(CONF_NAME)
    start = config.get(CONF_START)
    end = config.get(CONF_END)
    duration = config.get(CONF_DURATION)
    entities = config.get(CONF_ENTITIES)
    precision = config.get(CONF_PRECISION, config.get(CONF_ROUND_DIGITS))
    undef = config.get(CONF_PROCESS_UNDEF_AS)

    for template in [start, end]:
        if template is not None:
            template.hass = hass

    async_add_entities(
        [
            AverageSensor(
                hass,
                sensor_type,
                name,
                start,
                end,
                duration,
                entities,
                precision,
                undef,
            )
        ]
    )


def calc_median(values: dict) -> float:
    """Calculate median value."""
    keys = sorted(values.keys())
    val_a = val_b = idx_a = 0
    idx_b = len(keys) - 1

    while idx_a + 1 < idx_b:
        while idx_a < idx_b and val_a <= val_b:
            val_a += values[keys[idx_a]]
            idx_a += 1

        while idx_a < idx_b and val_a >= val_b:
            val_b += values[keys[idx_b]]
            idx_b -= 1

    if val_a > val_b:
        val_b += values[keys[idx_b]]
        if val_a == val_b:
            return (keys[idx_a] + keys[idx_b - 1]) / 2
        idx_a += 1
    elif val_b > val_a:
        val_a += values[keys[idx_a]]
        if val_a == val_b:
            return (keys[idx_a + 1] + keys[idx_b]) / 2
        idx_b += 1
    elif val_a == val_b:
        return (keys[idx_a] + keys[idx_b]) / 2

    return keys[idx_a] if val_a > val_b else keys[idx_b]


def calc_mode(values: dict) -> float:
    """Calculate mean value."""
    wmax = res = -1
    for value, weigth in values.items():
        if wmax < weigth:
            wmax = weigth
            res = value

    return res


# pylint: disable=r0902
class AverageSensor(Entity):
    """Implementation of an Average sensor."""

    # pylint: disable=r0913
    def __init__(
        self,
        hass,
        sensor_type: str,
        name: str,
        start,
        end,
        duration,
        entity_ids: list,
        precision: int = 2,
        undef: Optional[float] = None,
    ):
        """Initialize the sensor."""
        self._hass = hass
        self._sensor_type = sensor_type
        self._start_template = start
        self._end_template = end
        self._duration = duration
        self._period = self.start = self.end = None
        self._precision = precision
        self._undef = undef
        self._state = None
        self._unit_of_measurement = None
        self._icon = None
        self._temperature_mode = None

        if name:
            self._name = name
        else:
            name = next(v for k, v in SENSOR_TYPES.items() if self._sensor_type == v)
            self._name = f"{name} sensor".capitalize()

        self.sensors = expand_entity_ids(hass, entity_ids)
        self.count_sensors = len(self.sensors)
        self.available_sensors = 0
        self.count = 0
        self.min_value = self.max_value = self.mean = self.median = self.mode = None
        self.last = self.last_ts = None
        self.min_entity_id = self.max_entity_id = self.last_entity_id = None

    @property
    def _has_period(self) -> bool:
        """Return True if sensor has any period setting."""
        return (
            self._start_template is not None
            or self._end_template is not None
            or self._duration is not None
        )

    @property
    def should_poll(self) -> bool:
        """Return the polling state."""
        return self._has_period

    @property
    def name(self) -> Optional[str]:
        """Return the name of the sensor."""
        return self._name

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.available_sensors > 0 and self._has_state(self._state)

    @property
    def state(self) -> Union[None, str, int, float]:
        """Return the state of the sensor."""
        return self._state if self.available else STATE_UNAVAILABLE

    @property
    def unit_of_measurement(self) -> Optional[str]:
        """Return the unit of measurement of this entity."""
        return self._unit_of_measurement

    @property
    def icon(self) -> Optional[str]:
        """Return the icon to use in the frontend."""
        return self._icon

    @property
    def device_state_attributes(self) -> Optional[Dict[str, Any]]:
        """Return the state attributes of the sensor."""
        return {
            attr: getattr(self, attr)
            for attr in ATTR_TO_PROPERTY
            if getattr(self, attr) is not None
        }

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        # pylint: disable=unused-argument
        @callback
        def sensor_state_listener(entity, old_state, new_state):
            """Handle device state changes."""
            last_state = self._state
            self._update_state()
            if last_state != self._state:
                self.async_schedule_update_ha_state(True)

        # pylint: disable=unused-argument
        @callback
        def sensor_startup(event):
            """Update template on startup."""
            if self._has_period:
                self.async_schedule_update_ha_state(True)
            else:
                async_track_state_change(
                    self._hass, self.sensors, sensor_state_listener
                )
                sensor_state_listener(None, None, None)

        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, sensor_startup)

    @staticmethod
    def _has_state(state) -> bool:
        """Return True if state has any value."""
        return state is not None and state not in [
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
            "None",
        ]

    def _get_temperature(self, state: LazyState) -> Optional[float]:
        """Get temperature value from entity."""
        ha_unit = self._hass.config.units.temperature_unit
        domain = split_entity_id(state.entity_id)[0]
        if domain == WEATHER_DOMAIN:
            temperature = state.attributes.get("temperature")
            entity_unit = ha_unit
        elif domain in (CLIMATE_DOMAIN, WATER_HEATER_DOMAIN):
            temperature = state.attributes.get("current_temperature")
            entity_unit = ha_unit
        else:
            temperature = state.state
            entity_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if not self._has_state(temperature):
            return None

        temperature = convert_temperature(float(temperature), entity_unit, ha_unit)
        return temperature

    def _get_state_value(self, state: LazyState) -> Optional[float]:
        """Return value of given entity state and count some sensor attributes."""
        value = self._get_temperature(state) if self._temperature_mode else state.state
        if not self._has_state(value):
            return self._undef

        try:
            value = float(value)
        except ValueError:
            _LOGGER.error('Could not convert value "%s" to float', value)
            return None

        self.count += 1
        rvalue = round(value, self._precision)

        if self.min_value is None:
            self.min_value = self.max_value = rvalue
            self.min_entity_id = self.max_entity_id = state.entity_id
        else:
            if rvalue < self.min_value:
                self.min_value = rvalue
                self.min_entity_id = state.entity_id
            if rvalue > self.max_value:
                self.max_value = rvalue
                self.max_entity_id = state.entity_id

        if self.last_ts is None or self.last_ts < state.last_changed:
            self.last_ts = state.last_changed
            self.last = rvalue
            self.last_entity_id = state.entity_id

        return value

    @Throttle(UPDATE_MIN_TIME)
    def update(self):
        """Update the sensor state if it needed."""
        if self._has_period:
            self._update_state()

    @staticmethod
    def handle_template_exception(ex, field):
        """Log an error nicely if the template cannot be interpreted."""
        if ex.args and ex.args[0].startswith("UndefinedError: 'None' has no attribute"):
            # Common during HA startup - so just a warning
            _LOGGER.warning(ex)
            return
        _LOGGER.error("Error parsing template for field %s", field)
        _LOGGER.error(ex)

    def _update_period(self):  # pylint: disable=r0912
        """Parse the templates and calculate a datetime tuples."""
        start = end = None
        now = dt_util.now()

        # Parse start
        _LOGGER.debug("Process start template: %s", self._start_template)
        if self._start_template is not None:
            try:
                start_rendered = self._start_template.render()
            except (TemplateError, TypeError) as ex:
                self.handle_template_exception(ex, "start")
                return
            start = dt_util.parse_datetime(start_rendered)
            if start is None:
                try:
                    start = dt_util.as_local(
                        dt_util.utc_from_timestamp(math.floor(float(start_rendered)))
                    )
                except ValueError:
                    _LOGGER.error(
                        "Parsing error: start must be a datetime or a timestamp"
                    )
                    return

        # Parse end
        _LOGGER.debug("Process end template: %s", self._end_template)
        if self._end_template is not None:
            try:
                end_rendered = self._end_template.render()
            except (TemplateError, TypeError) as ex:
                self.handle_template_exception(ex, "end")
                return
            end = dt_util.parse_datetime(end_rendered)
            if end is None:
                try:
                    end = dt_util.as_local(
                        dt_util.utc_from_timestamp(math.floor(float(end_rendered)))
                    )
                except ValueError:
                    _LOGGER.error(
                        "Parsing error: end must be a datetime or a timestamp"
                    )
                    return

        # Calculate start or end using the duration
        _LOGGER.debug("Process duration: %s", self._duration)
        if self._duration is not None:
            if start is None:
                if end is None:
                    end = now
                start = end - self._duration
            else:
                end = start + self._duration

        _LOGGER.debug("Start: %s, End: %s", start, end)
        if start is None or end is None:
            return

        if start > now:
            # History hasn't been written yet for this period
            return
        if now < end:
            # No point in making stats of the future
            end = now

        self._period = start, end
        self.start = start.replace(microsecond=0).isoformat()
        self.end = end.replace(microsecond=0).isoformat()

    def _update_state(self):  # pylint: disable=r0914,r0912,r0915
        """Update the sensor state."""
        _LOGGER.debug('Updating sensor "%s"', self.name)
        start = end = start_ts = end_ts = None
        p_period = self._period

        # Parse templates
        self._update_period()

        if self._period is not None:
            now = datetime.datetime.now()
            start, end = self._period
            if p_period is None:
                p_start = p_end = now
            else:
                p_start, p_end = p_period

            # Convert times to UTC
            start = dt_util.as_utc(start)
            end = dt_util.as_utc(end)
            p_start = dt_util.as_utc(p_start)
            p_end = dt_util.as_utc(p_end)

            # Compute integer timestamps
            now_ts = math.floor(dt_util.as_timestamp(now))
            start_ts = math.floor(dt_util.as_timestamp(start))
            end_ts = math.floor(dt_util.as_timestamp(end))
            p_start_ts = math.floor(dt_util.as_timestamp(p_start))
            p_end_ts = math.floor(dt_util.as_timestamp(p_end))

            # If period has not changed and current time after the period end..
            if start_ts == p_start_ts and end_ts == p_end_ts and end_ts <= now_ts:
                # Don't compute anything as the value cannot have changed
                return

        self.available_sensors = 0
        norm_vals = []
        values = {}
        self.count = 0
        self.min_value = self.max_value = self.mean = self.median = self.mode = None
        self.last = self.last_entity_id = self.last_ts = None

        def add_value(value: float, weight: int = 1):
            val = values.get(value, 0.0)
            values[value] = val + weight

        # pylint: disable=too-many-nested-blocks
        for entity_id in self.sensors:
            _LOGGER.debug('Processing entity "%s"', entity_id)

            state = self._hass.states.get(entity_id)  # type: LazyState

            if state is None:
                _LOGGER.error('Unable to find an entity "%s"', entity_id)
                continue

            if self._temperature_mode is None:
                domain = split_entity_id(state.entity_id)[0]
                self._temperature_mode = (
                    domain in (WEATHER_DOMAIN, CLIMATE_DOMAIN, WATER_HEATER_DOMAIN)
                    or state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
                    in TEMPERATURE_UNITS
                )
                if self._temperature_mode:
                    _LOGGER.debug("%s is a temperature entity.", entity_id)
                    self._unit_of_measurement = self._hass.config.units.temperature_unit
                    self._icon = "mdi:thermometer"
                else:
                    _LOGGER.debug("%s is NOT a temperature entity.", entity_id)
                    self._unit_of_measurement = state.attributes.get(
                        ATTR_UNIT_OF_MEASUREMENT
                    )
                    self._icon = state.attributes.get(ATTR_ICON)

            sum_val = 0
            elapsed = 0

            if self._period is None:
                # Get current state
                sum_val = self._get_state_value(state)
                _LOGGER.debug("Current state: %s", sum_val)
                add_value(sum_val)

            else:
                # Get history between start and now
                history_list = history.state_changes_during_period(
                    self.hass, start, end, str(entity_id)
                )

                if entity_id not in history_list.keys():
                    sum_val = self._get_state_value(state)
                    _LOGGER.warning(
                        'Historical data not found for entity "%s". '
                        "Current state used: %s",
                        entity_id,
                        sum_val,
                    )
                    add_value(sum_val)
                else:
                    # Get the first state
                    item = history.get_state(self.hass, start, entity_id)
                    _LOGGER.debug("Initial historical state: %s", item)
                    last_state = None
                    last_time = start_ts
                    if item is not None and self._has_state(item.state):
                        last_state = self._get_state_value(item)
                        add_value(last_state)

                    # Get the other states
                    for item in history_list.get(entity_id):
                        _LOGGER.debug("Historical state: %s", item)
                        if self._has_state(item.state):
                            current_state = self._get_state_value(item)
                            current_time = item.last_changed.timestamp()

                            if last_state is not None:
                                last_elapsed = current_time - last_time
                                sum_val += last_state * last_elapsed
                                add_value(last_state, last_elapsed)
                                elapsed += last_elapsed

                            last_state = current_state
                            last_time = current_time

                    # Count time elapsed between last history state and now
                    if last_state is not None:
                        last_elapsed = end_ts - last_time
                        sum_val += last_state * last_elapsed
                        add_value(last_state, last_elapsed)
                        elapsed += last_elapsed

                    if elapsed:
                        sum_val /= elapsed
                    _LOGGER.debug("Historical average state: %s", sum_val)

            if isinstance(sum_val, numbers.Number):
                norm_vals.append(sum_val)
                self.available_sensors += 1

        self.mean = (
            round(sum(norm_vals) / len(norm_vals), self._precision)
            if norm_vals
            else None
        )
        self.median = round(calc_median(values), self._precision) if norm_vals else None
        self.mode = round(calc_mode(values), self._precision) if norm_vals else None

        self._state = getattr(
            self, next(k for k, v in SENSOR_TYPES.items() if self._sensor_type == v)
        )
        _LOGGER.debug("New state (%s): %s", self._sensor_type, self._state)
