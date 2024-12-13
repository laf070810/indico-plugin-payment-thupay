from indico.core.plugins import IndicoPluginBlueprint

from indico_payment_thupay.controllers import RHTHUpayNotify, RHTHUpaySuccess

blueprint = IndicoPluginBlueprint(
    "payment_thupay",
    __name__,
    url_prefix="/event/<int:event_id>/registrations/<int:reg_form_id>/payment/response/thupay",
)

# sync return
blueprint.add_url_rule("/success", "success", RHTHUpaySuccess, methods=("GET", "POST"))

# async return
blueprint.add_url_rule("/notify", "notify", RHTHUpayNotify, methods=("POST",))
