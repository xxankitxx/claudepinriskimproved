"""VAJRA computation engine (spec §5) — one module per spec sub-section.

Each module is independent and testable:
  §5.1  basis_engine            -> BasisResult
  §5.2  pin_locator             -> PinResult
  §5.3  trigger_engine          -> TriggerBand
  §5.4  freshness_engine        -> FreshnessEngine (stateful)
  §5.5  basis_monitor           -> BasisMonitor (stateful)
  §5.6  decision_maker          -> DecisionMaker (stateful, the gate)
  §5.7  continuation_validator  -> ContinuationValidator (stateful)
"""
