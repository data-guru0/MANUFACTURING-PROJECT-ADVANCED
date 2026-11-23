"""Fields only. The legacy system has no business logic of its own beyond stock Odoo.

x_seed_batch tags every synthetic record so it can be audited and wiped.
"""

from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    x_drawing_no = fields.Char("Drawing No.")
    # Free text on purpose: estimators type it however they like (W4).
    x_material_grade = fields.Char("Material Grade")
    x_part_family = fields.Char("Part Family")
    x_density_g_cm3 = fields.Float("Density (g/cm³)", digits=(6, 3))
    x_seed_batch = fields.Char("Seed Batch", index=True)


class MrpWorkcenter(models.Model):
    _inherit = "mrp.workcenter"

    x_max_x_mm = fields.Float("Max X (mm)")
    x_max_y_mm = fields.Float("Max Y (mm)")
    x_max_z_mm = fields.Float("Max Z (mm)")
    x_max_weight_kg = fields.Float("Max Workpiece Weight (kg)")
    x_best_tolerance_mm = fields.Float("Best Tolerance (± mm)", digits=(6, 4))
    x_best_ra = fields.Float("Best Surface Finish Ra (µm)", digits=(6, 2))
    x_power_kw = fields.Float("Average Power Draw (kW)")
    x_seed_batch = fields.Char("Seed Batch", index=True)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    x_rfq_received_at = fields.Datetime("RFQ Received At")
    x_quote_sent_at = fields.Datetime("Quote Sent At")
    x_lost_reason = fields.Char("Lost Reason")
    x_seed_batch = fields.Char("Seed Batch", index=True)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    x_seed_batch = fields.Char("Seed Batch", index=True)


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    x_seed_batch = fields.Char("Seed Batch", index=True)


class ResPartner(models.Model):
    _inherit = "res.partner"

    x_nda_restricted = fields.Boolean("NDA Restricted")
    x_required_certs = fields.Char("Required Certificates")
    x_language = fields.Char("Correspondence Language")
    x_seed_batch = fields.Char("Seed Batch", index=True)
