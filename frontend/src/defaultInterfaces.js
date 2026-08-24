// Built-in per-PHY interface definitions (2026-08-23 digital-arch spec):
// the SerDes RX worked example ships as the "ser-des" default; the other
// PHYs get smaller starters (clocks, resets, headline signals). ddr/hbm
// reuse the LPDDR (DFI-style) default in v1, same aliasing rule as
// DIGITAL_ARCHITECTURES.
//
// The JSON files are the single source of truth, shared with the backend
// test suite (backend/test_digital_if.py round-trips each of them through
// the validator: zero hard errors, zero soft warnings). Edit the JSON, not
// this module.

import serdes from "./defaultInterfaces/ser-des.json";
import lpddr from "./defaultInterfaces/lpddr.json";
import dieToDie from "./defaultInterfaces/die-to-die.json";
import optical from "./defaultInterfaces/optical.json";

export const DEFAULT_INTERFACES = {
  "ser-des": serdes,
  "lpddr": lpddr,
  "ddr": lpddr,
  "hbm": lpddr,
  "die-to-die": dieToDie,
  "optical": optical,
};
