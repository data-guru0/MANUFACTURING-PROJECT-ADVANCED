# Data quality report for Apex (S2)

_Generated 2026-11-23 10:50 by `apex_agent.knowledge.report` from the agent's read model. **Nothing here was changed in Odoo**: Odoo stays the system of record; each finding is a recommendation._

## 1. Duplicate customers (W4)
5 customer records look like a second copy of another one. Quotes are split between them, so history per customer is wrong.

| Duplicate | Name | Same as | Name | Evidence |
|---|---|---|---|---|
| CUST-001-DUP | Kestrel Automation Inc. | CUST-001 | Kestrel Automation | name similarity 1.00; same email |
| CUST-002-DUP | NorthBridge Pumps Ltd | CUST-002 | Northbridge Pumps | name similarity 1.00 |
| CUST-006-DUP | Corvane Hydraulic | CUST-006 | Corvane Hydraulics | name similarity 0.97; same email |
| CUST-019-DUP | VECTRA Motion-Control | CUST-019 | Vectra Motion Control | name similarity 1.00 |
| CUST-031-DUP | Kraemer Antriebstechnik GmbH | CUST-031 | Krämer Antriebstechnik | name similarity 1.00 |

**Recommendation:** merge each pair in Odoo (Contacts → select both → Action → Merge), keeping the older record.

## 2. Material written many ways (W4)
The free-text material field on products holds 35 different spellings. The read model maps each to one code (from the part's BOM first, else the reviewed lookup in `agent/config/material_map.yaml`).

| Material | Spellings found |
|---|---|
| MAT-1018 | 1018, C1018, CRS 1018, Steel 1018 |
| MAT-AL6061 | 6061 T6, 6061-T6, AL6061, Al 6061-T6, Alu 6061, Aluminium 6061 |
| MAT-AL7075 | 7075-T6, AL7075, Al 7075-T6, Alu 7075 |
| MAT-C360 | Brass, Brass C360, C360, CuZn39Pb3 |
| MAT-POM | Delrin, POM, POM-C |
| MAT-S235 | Mild steel, S235, S235JR, Steel S235 |
| MAT-SS304 | 1.4301, 304 SS, AISI 304, SS304, Stainless 304 |
| MAT-SS316 | 1.4401, 316L, AISI 316, SS316 |
| MAT-TI64 | Ti-6Al-4V |

Spellings that contradict the BOM: **0**. Spellings not yet in the reviewed list: **0**.

**Recommendation:** replace the free-text field with a fixed list (or the BOM material) so it can't drift again.

## 3. Products without a routing (W4)
53 of 353 quoted products have **no routing** in Odoo. For quoting, the read model estimates one from the closest analog (same family and material) and marks it `estimated:` everywhere it is shown (53 estimated).

| Product | Routing used |
|---|---|
| D-10004-A | estimated:D-10068-A |
| D-10005-A | estimated:D-10211-A |
| D-10007-A | estimated:D-10036-B |
| D-10008-A | estimated:D-10097-A |
| D-10009-A | estimated:D-10097-A |
| D-10014-A | estimated:D-10293-A |
| D-10014-B | estimated:D-10293-A |
| D-10020-B | estimated:D-10036-B |
| D-10031-B | estimated:D-10048-A |
| D-10035-A | estimated:D-10036-B |
| … | (43 more) |

**Recommendation:** Kavita adds the real routing to these BOMs; estimates disappear as routings appear.

## 4. Stale price list on the share (W5)
The calculator's material prices disagree with Odoo. **The agent always uses Odoo**; each conflict is logged.

| Material | Share $/kg | Odoo $/kg | Difference |
|---|---|---|---|
| MAT-AL7075 | 9.41 | 12.00 | -21.6 % |
| MAT-TI64 | 35.30 | 45.00 | -21.6 % |
| MAT-SS304 | 4.31 | 5.50 | -21.6 % |
| MAT-AL6061 | 5.10 | 6.50 | -21.5 % |
| MAT-SS316 | 6.28 | 8.00 | -21.5 % |
| MAT-C360 | 8.63 | 11.00 | -21.5 % |
| MAT-1018 | 1.73 | 2.20 | -21.4 % |
| MAT-S235 | 1.26 | 1.60 | -21.3 % |
| MAT-POM | 6.66 | 7.00 | -4.9 % |

**Recommendation:** retire `Material_Prices_2023.xlsx` and the calculator's price table; Tom maintains prices in Odoo only.

## 5. Quotes that exist only in Excel (W5)
30 quote files on the share have no Odoo quotation, so nobody searching Odoo can find them. The read model indexes them as analogs (marked `excel_only`).

| Customer folder | Drawing | Quoted unit price |
|---|---|---|
| Ambrose Printing Systems | D-15012 Rev A | 33.22 |
| Ambrose Printing Systems | D-15025 Rev A | 46.57 |
| Bharat Precision Motors | D-15018 Rev A | 99.40 |
| Bharat Precision Motors | D-15027 Rev A | 129.84 |
| Bluefin Marine Systems | D-15021 Rev A | 55.68 |
| Bluefin Marine Systems | D-15029 Rev A | 46.82 |
| Cobalt Energy Storage | D-15003 Rev A | 4305.52 |
| Corvane Hydraulics | D-15009 Rev A | 1175.37 |
| Foxglove Bio Instruments | D-15026 Rev A | 48.02 |
| Granite Peak Mining Equipment | D-15007 Rev A | 89.79 |
| Granite Peak Mining Equipment | D-15015 Rev A | 76.99 |
| Granite Peak Mining Equipment | D-15028 Rev A | 125.81 |
| Harrow Tractor Parts | D-15006 Rev A | 58.94 |
| Industrias Montoya | D-15014 Rev A | 68.02 |
| Kestrel Automation | D-15013 Rev A | 209.32 |
| Krämer Antriebstechnik | D-15008 Rev A | 56.52 |
| Krämer Antriebstechnik | D-15016 Rev A | 149.42 |
| Mariner Valve Co | D-15030 Rev A | 116.78 |
| Pinecrest Packaging Machines | D-15011 Rev A | 14.81 |
| Rookfield Rail Systems | D-15017 Rev A | 44.14 |
| Talleres Ortega | D-15001 Rev A | 110.61 |
| Talleres Ortega | D-15010 Rev A | 152.13 |
| Talleres Ortega | D-15020 Rev A | 38.44 |
| Thornbury Test Rigs | D-15002 Rev A | 201.13 |
| Valdera Robotics | D-15005 Rev A | 73.01 |
| Valdera Robotics | D-15019 Rev A | 15.52 |
| Vectra Motion Control | D-15022 Rev A | 59.47 |
| Wexford Instruments | D-15023 Rev A | 47.25 |
| Yarrow Lab Automation | D-15024 Rev A | 113.16 |
| Zephyr HVAC Products | D-15004 Rev A | 877.70 |

## 6. Sync status

| Source | Last run | Read | Changed |
|---|---|---|---|
| odoo:mrp.bom | 2026-11-23 10:49 | 153 | 0 |
| odoo:mrp.bom.line | 2026-11-23 10:49 | 153 | 0 |
| odoo:mrp.production | 2026-11-23 10:49 | 1 | 0 |
| odoo:mrp.routing.workcenter | 2026-11-23 10:49 | 482 | 0 |
| odoo:mrp.workcenter | 2026-11-23 10:49 | 8 | 0 |
| odoo:mrp.workorder | 2026-11-23 10:49 | 34 | 0 |
| odoo:product.product | 2026-11-23 10:49 | 3 | 0 |
| odoo:product.supplierinfo | 2026-11-23 10:49 | 1 | 0 |
| odoo:product.template | 2026-11-23 10:49 | 3 | 0 |
| odoo:purchase.order | 2026-11-23 10:49 | 1 | 0 |
| odoo:res.partner | 2026-11-23 10:49 | 1 | 0 |
| odoo:sale.order | 2026-11-23 10:49 | 400 | 0 |
| odoo:sale.order.line | 2026-11-23 10:49 | 400 | 0 |
| odoo:stock.quant | 2026-11-23 10:49 | 1 | 0 |
| share | 2026-11-23 10:49 | 150 | - |
