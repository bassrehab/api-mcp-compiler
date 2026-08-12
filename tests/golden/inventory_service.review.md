# Tool surface review: Synthetic Inventory Service

Every decision below is a proposal unless it is marked applied. A planner decision
carries a confidence below 1.0; a decision a reviewer already recorded carries 1.0.

## Summary

| Item | Count |
|---|---|
| Source operations | 3 |
| Planned artifacts | 2 |
| Decisions | 12 |
| Awaiting a reviewer | 0 |
| Blocking ambiguities | 0 |

## Planned surface

| Name | Kind | Risk | Group | Suitability | Review |
|---|---|---|---|---|---|
| `list_items_held_warehouse` | tool | read | warehouses | 1.0 | proposed |
| `permanently_remove_item_record_warehouse` | tool | destructive | warehouses | 0.6667 | rejected |

## Proposed omissions

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `listWarehouseItemsLegacy` |  | human | 1.0 | applied | Omission accepted by a reviewer in the overlay. |
| `listWarehouseItemsLegacy` |  | planner | 0.6 | applied | The specification marks this operation deprecated, so exposing it spends agent attention on a surface the provider intends to withdraw. |

## Surface kind changes

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `listWarehouseItemsLegacy` | `tool` to `resource` | planner | 0.6 | applied | A read whose only inputs identify what to fetch is addressable, so a resource avoids spending a tool slot on a lookup. |

## Renames

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `listWarehouseItems` | `listWarehouseItems` to `list_items_held_warehouse` | planner | 0.75 | applied | Derived from the operation summary 'List items held in a warehouse' rather than the source identifier 'listWarehouseItems', which names a tool after the API instead of the task. |
| `listWarehouseItemsLegacy` | `listWarehouseItemsLegacy` to `list_items_using_retired_v1` | planner | 0.75 | applied | Derived from the operation summary 'List items using the retired v1 projection' rather than the source identifier 'listWarehouseItemsLegacy', which names a tool after the API instead of the task. |
| `purgeWarehouseItems` | `purgeWarehouseItems` to `permanently_remove_item_record_warehouse` | planner | 0.75 | applied | Derived from the operation summary 'Permanently remove every item record for a warehouse' rather than the source identifier 'purgeWarehouseItems', which names a tool after the API instead of the task. |

## Output projections

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `listWarehouseItems` | `page` | planner | 0.7 | applied | Withholds page carry transport rather than task concerns. Each is optional and is left off the wire, so the service applies its own value. Confirm no caller needs to set them explicitly. |

## Grouping

| Target | Change | Origin | Confidence | Status | Rationale |
|---|---|---|---|---|---|
| `listWarehouseItems` | `-` to `warehouses` | planner | 0.65 | applied | Grouped by the first path segment 'warehouses', which is the coarsest grouping the specification states rather than one this planner invents. |
| `listWarehouseItemsLegacy` | `-` to `warehouses` | planner | 0.65 | applied | Grouped by the first path segment 'warehouses', which is the coarsest grouping the specification states rather than one this planner invents. |
| `purgeWarehouseItems` | `-` to `warehouses` | planner | 0.65 | applied | Grouped by the first path segment 'warehouses', which is the coarsest grouping the specification states rather than one this planner invents. |

## What a reviewer must decide

Every proposal has been recorded in the overlay.

Decisions already accepted by a reviewer: 1.
