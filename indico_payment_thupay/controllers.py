import json
from itertools import chain

import requests
from flask import flash, redirect, request
from flask_pluginengine import current_plugin
from indico.modules.events.payment.models.transactions import TransactionAction
from indico.modules.events.payment.notifications import notify_amount_inconsistency
from indico.modules.events.payment.util import register_transaction
from indico.modules.events.registration.models.registrations import Registration
from indico.web.flask.util import url_for
from indico.web.rh import RH
from werkzeug.exceptions import BadRequest

from indico_payment_thupay import _

paypal_transaction_action_mapping = {
    "TRADE_SUCCESS": TransactionAction.complete,
    "TRADE_FAIL": TransactionAction.reject,
    # "Pending": TransactionAction.pending,
}


class RHTHUpayNotify(RH):
    """Process the notification (async return) sent by the THUpay"""

    CSRF_ENABLED = False

    def _process_args(self):
        self.token = request.args["token"]
        self.registration = Registration.query.filter_by(uuid=self.token).first()
        if not self.registration:
            raise BadRequest
        self.biz_content = json.loads(request.form.get("bizContent"))

    def _process(self):
        # -------- verify business --------
        # self._verify_business()

        # -------- verify params --------
        # verify_params = list(chain(IPN_VERIFY_EXTRA_PARAMS, request.form.items()))
        # result = requests.post(
        #     current_plugin.settings.get("url"), data=verify_params
        # ).text
        # if result != "VERIFIED":
        #     current_plugin.logger.warning(
        #         "Paypal IPN string %s did not validate (%s)", verify_params, result
        #     )
        #     return

        # -------- verify duplicated transaction --------
        if self._is_transaction_duplicated():
            current_plugin.logger.info(
                "Payment not recorded because transaction was duplicated\nData received: %s",
                request.form,
            )
            return

        # -------- verify payment status --------
        payment_status = self.biz_content["tradeState"]
        if payment_status == "TRADE_FAIL":
            current_plugin.logger.info(
                "Payment failed (status: %s)\nData received: %s",
                payment_status,
                request.form,
            )
            return
        if (
            payment_status == "TRADE_FULL_REFUND"
            or float(self.biz_content["tradeAmount"]) <= 0
        ):
            current_plugin.logger.warning(
                "Payment refunded (status: %s)\nData received: %s",
                payment_status,
                request.form,
            )
            return
        if payment_status not in paypal_transaction_action_mapping:
            current_plugin.logger.warning(
                "Payment status '%s' not recognized\nData received: %s",
                payment_status,
                request.form,
            )
            return

        # -------- verify amount --------
        self._verify_amount()

        # -------- register transaction --------
        register_transaction(
            registration=self.registration,
            amount=float(self.biz_content["tradeAmount"]),
            currency=self.biz_content["moneyTypeId"],
            action=paypal_transaction_action_mapping[payment_status],
            provider="thupay",
            data=request.form,
        )

    def _verify_amount(self):
        expected_amount = float(self.registration.price)
        expected_currency = self.registration.currency
        amount = float(self.biz_content["tradeAmount"])
        currency = self.biz_content["moneyTypeId"]

        # handle the case where moneyTypeId in the request is "0"
        if currency != "0":
            condition = expected_amount == amount and expected_currency == currency
        else:
            condition = expected_amount == amount

        if condition:
            return True
        current_plugin.logger.warning(
            "Payment doesn't match event's fee: %s %s != %s %s",
            amount,
            currency,
            expected_amount,
            expected_currency,
        )
        notify_amount_inconsistency(self.registration, amount, currency)
        return False

    def _is_transaction_duplicated(self):
        transaction = self.registration.transaction
        if not transaction or transaction.provider != "thupay":
            return False

        # biz_content is from database. self.biz_content is from current request. We compare them
        biz_content = json.loads(transaction.data["bizContent"])
        return (
            biz_content["tradeState"] == self.biz_content["tradeState"]
            and biz_content["outTradeNo"] == self.biz_content["outTradeNo"]
        )


class RHTHUpaySuccess(RHTHUpayNotify):
    """Confirmation message after successful payment"""

    def _process(self):
        flash(_("Your payment request has been processed."), "success")
        return redirect(
            url_for(
                "event_registration.display_regform",
                self.registration.locator.registrant,
            )
        )
