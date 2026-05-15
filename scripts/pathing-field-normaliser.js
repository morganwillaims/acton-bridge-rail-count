// Acton Bridge pathing-field-normaliser.js
// Shared helpers for schedule/vstp loader and injector scripts.

function clean(value) {
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function firstNonEmpty(...values) {
  for (const value of values) {
    const v = clean(value);
    if (v) return v;
  }
  return "";
}

function normalisePowerCode(raw) {
  const v = clean(raw).toUpperCase();
  if (!v) return "unknown";
  if (["D", "DSL", "DIESEL", "DMU", "DIESEL_LOCO", "DIESEL LOCOMOTIVE"].includes(v)) return "diesel";
  if (["E", "ELEC", "ELECTRIC", "EMU", "ELECTRIC_LOCO", "ELECTRIC LOCOMOTIVE"].includes(v)) return "electric";
  if (["DE", "ED", "BI", "BI-MODE", "BIMODE", "DIESEL/ELECTRIC", "ELECTRIC/DIESEL"].includes(v)) return "diesel_electric";
  if (/DIESEL/.test(v) && /ELECTRIC|ELEC/.test(v)) return "diesel_electric";
  if (/DIESEL|DMU|DSL/.test(v)) return "diesel";
  if (/ELECTRIC|EMU|ELEC/.test(v)) return "electric";
  return "unknown";
}

function pathingLabel(power) {
  switch (power) {
    case "diesel": return "Pathed as diesel loco";
    case "electric": return "Pathed as electric loco";
    case "diesel_electric": return "Pathed diesel/electric";
    default: return "Pathing unknown";
  }
}

function extractPathingFields(source = {}) {
  const powerType = firstNonEmpty(source.power_type, source.power, source.powerCode, source.power_code);
  const plannedPower = firstNonEmpty(source.planned_power, source.plannedPower, source.traction_power, source.tractionPower);
  const tractionType = firstNonEmpty(source.traction_type, source.tractionType, source.traction);
  const tractionClass = firstNonEmpty(source.traction_class, source.tractionClass, source.timing_class);
  const timingLoad = firstNonEmpty(source.timing_load, source.timingLoad, source.load, source.trailing_load);
  const operatingCharacteristics = firstNonEmpty(source.operating_characteristics, source.operatingCharacteristics, source.op_chars, source.characteristics);
  const stockType = firstNonEmpty(source.stock_type, source.stockType, source.stock);
  const speed = firstNonEmpty(source.speed, source.planned_speed, source.max_speed);

  const power = normalisePowerCode(firstNonEmpty(
    source.pathing_power,
    powerType,
    plannedPower,
    tractionType,
    tractionClass,
    timingLoad,
    stockType,
    operatingCharacteristics
  ));

  return {
    pathing_power: power,
    pathing_power_label: pathingLabel(power),
    pathing_power_source: power === "unknown" ? "source_unknown" : "schedule_vstp_capture",
    power_type: powerType,
    planned_power: plannedPower,
    traction_type: tractionType,
    traction_class: tractionClass,
    timing_load: timingLoad,
    operating_characteristics: operatingCharacteristics,
    stock_type: stockType,
    speed
  };
}

module.exports = { clean, firstNonEmpty, normalisePowerCode, pathingLabel, extractPathingFields };
