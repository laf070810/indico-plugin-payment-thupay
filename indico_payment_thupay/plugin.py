import json
import time

import requests
from flask_pluginengine import render_plugin_template
from indico.core.logger import Logger
from indico.core.plugins import IndicoPlugin, url_for_plugin
from indico.modules.events.payment import (
    PaymentEventSettingsFormBase,
    PaymentPluginMixin,
    PaymentPluginSettingsFormBase,
)
from indico.modules.events.registration.models.registrations import (
    Registration,
    RegistrationState,
)
from indico.util.string import remove_accents, str_to_ascii
from indico.web.forms.validators import UsedIf
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from wtforms.fields import IntegerField, StringField, URLField
from wtforms.validators import DataRequired, Optional, Regexp

from indico_payment_thupay import _
from indico_payment_thupay.blueprint import blueprint
from indico_payment_thupay.util import RsaUtil


class PluginSettingsForm(PaymentPluginSettingsFormBase):
    url = URLField(
        _("API URL"), [DataRequired()], description=_("URL of the THUpay HTTP API.")
    )
    url_time = URLField(
        _("TIME SERVER URL"),
        [DataRequired()],
        description=_("URL of the timestamp server for THUpay."),
    )
    payment_item_id = StringField(
        _("payment_item_id"),
        [Optional()],
        description=_(
            "The project number. Event managers will be able to override this."
        ),
    )
    payment_item_key = StringField(
        _("payment_item_key"),
        [Optional()],
        description=_(
            "The private key of the project. Event managers will be able to override this."
        ),
    )


class EventSettingsForm(PaymentEventSettingsFormBase):
    payment_item_id = StringField(
        _("payment_item_id"),
        [UsedIf(lambda form, _: form.enabled.data), DataRequired()],
        description=_("The project number."),
    )
    payment_item_key = StringField(
        _("payment_item_key"),
        [UsedIf(lambda form, _: form.enabled.data), DataRequired()],
        description=_("The private key of the project."),
    )
    allowed_registration_form_ids = StringField(
        _("allowed_registration_form_ids"),
        [
            UsedIf(lambda form, _: form.enabled.data),
            Regexp(r"\[\d+( *, *\d+)*\]"),
            Optional(),
        ],
        description=_(
            "(whitelist) JSON string of non-empty list of IDs of the registration forms which are allowed to use this payment method. Registration forms that are not in this list are not allowed. Empty string for no requirement. Actual allowed registration forms are the intersection of the allowed ones of allowed_registration_form_ids and disallowed_registration_form_ids."
        ),
    )
    disallowed_registration_form_ids = StringField(
        _("disallowed_registration_form_ids"),
        [
            UsedIf(lambda form, _: form.enabled.data),
            Regexp(r"\[\d+( *, *\d+)*\]"),
            Optional(),
        ],
        description=_(
            "(blacklist) JSON string of non-empty list of IDs of the registration forms which are not allowed to use this payment method. Registration forms that are not in this list are allowed. Empty string for no requirement. Actual allowed registration forms are the intersection of the allowed ones of allowed_registration_form_ids and disallowed_registration_form_ids."
        ),
    )
    completed_registration_form_id = IntegerField(
        _("completed_registration_form_id"),
        [UsedIf(lambda form, _: form.enabled.data), Optional()],
        description=_(
            "ID of the registration form which is required to be completed before the payment. Empty for no requirement. Currently only one completed registration form is supported."
        ),
    )
    uncompleted_registration_form_id = IntegerField(
        _("uncompleted_registration_form_id"),
        [UsedIf(lambda form, _: form.enabled.data), Optional()],
        description=_(
            "ID of the registration form which is required to be uncompleted before the payment. Empty for no requirement. Currently only one uncompleted registration form is supported."
        ),
    )
    custom_payment_name = StringField(
        _("custom_payment_name"),
        [UsedIf(lambda form, _: form.enabled.data), Optional()],
        description=_(
            "Custom payment name. Used in tradeName and tradeSummary. If empty, the title of the event will be used. "
        ),
    )


class THUpayPaymentPlugin(PaymentPluginMixin, IndicoPlugin):
    """THUpay

    Plugin for integrating THUpay as a payment method in Indico.
    """

    configurable = True
    settings_form = PluginSettingsForm
    event_settings_form = EventSettingsForm
    default_settings = {
        "method_name": "THUpay",
        "url": "https://fa-online.tsinghua.edu.cn/zjjsfw/zjjs/v3/api.do",
        "url_time": "https://fa-online.tsinghua.edu.cn/zjjsfw/zjjs/v3/gettime.do",
        "payment_item_id": "",
        "payment_item_key": "",
    }
    default_event_settings = {
        "enabled": False,
        "method_name": None,
        "payment_item_id": None,
        "payment_item_key": None,
        "allowed_registration_form_ids": "",
        "disallowed_registration_form_ids": "",
        "completed_registration_form_id": None,
        "uncompleted_registration_form_id": None,
        "custom_payment_name": "",
    }

    def init(self):
        super().init()

    @property
    def logo_url(self):
        return url_for_plugin(self.name + ".static", filename="images/logo.png")

    def get_blueprints(self):
        return blueprint

    def adjust_payment_form_data(self, data):
        settings = data["settings"]
        event_settings = data["event_settings"]
        event = data["event"]
        registration = data["registration"]
        plain_name = remove_accents(registration.full_name)
        plain_title = remove_accents(
            event.title
            if event_settings["custom_payment_name"] == ""
            else event_settings["custom_payment_name"]
        )
        amount = data["amount"]
        currency = data["currency"]

        # -------- deal with allowed_registration_form_ids and disallowed_registration_form_ids --------
        allowed_registration_form_ids = (
            json.loads(event_settings["allowed_registration_form_ids"])
            if event_settings["allowed_registration_form_ids"] != ""
            else None
        )
        disallowed_registration_form_ids = (
            json.loads(event_settings["disallowed_registration_form_ids"])
            if event_settings["disallowed_registration_form_ids"] != ""
            else []
        )

        if allowed_registration_form_ids is not None:
            if registration.registration_form_id not in allowed_registration_form_ids:
                data["payment_allowed"] = False
                data["message"] = (
                    "Payment method not allowed in this registration form! Please use appropriate methods. "
                )
                return
            elif registration.registration_form_id in disallowed_registration_form_ids:
                data["payment_allowed"] = False
                data["message"] = (
                    "Payment method not allowed in this registration form! Please use appropriate methods. "
                )
                return
            else:
                data["payment_allowed"] = True
        elif registration.registration_form_id in disallowed_registration_form_ids:
            data["payment_allowed"] = False
            data["message"] = (
                "Payment method not allowed in this registration form! Please use appropriate methods. "
            )
            return
        else:
            data["payment_allowed"] = True

        # -------- deal with completed_registration_form_id --------
        completed_registration_form_id = event_settings[
            "completed_registration_form_id"
        ]

        if (completed_registration_form_id is not None) and (
            completed_registration_form_id != registration.registration_form_id
        ):
            try:
                related_registration = Registration.query.filter(
                    Registration.is_active,
                    # Registration.first_name == registration.first_name,
                    # Registration.last_name == registration.last_name,
                    Registration.email == registration.email,
                    Registration.registration_form_id == completed_registration_form_id,
                ).one()
            except NoResultFound:
                data["payment_allowed"] = False
                data["message"] = (
                    "No related registration found! Please refer to the notices and complete the related registration first. "
                )
                return
            except MultipleResultsFound:
                data["payment_allowed"] = False
                data["message"] = (
                    "Multiple registrations with the same email in the related registration found! Please contact the organizers to resolve the conflict. "
                )
                return
            else:
                if related_registration.state != RegistrationState.complete:
                    data["payment_allowed"] = False
                    data["message"] = (
                        "Related registration has not been completed. Please refer to the notices and complete the related registration first."
                    )
                    return
                else:
                    data["payment_allowed"] = True
        else:
            data["payment_allowed"] = True

        # -------- deal with uncompleted_registration_form_id --------
        uncompleted_registration_form_id = event_settings[
            "uncompleted_registration_form_id"
        ]

        if (uncompleted_registration_form_id is not None) and (
            uncompleted_registration_form_id != registration.registration_form_id
        ):
            try:
                related_registration = Registration.query.filter(
                    Registration.is_active,
                    # Registration.first_name == registration.first_name,
                    # Registration.last_name == registration.last_name,
                    Registration.email == registration.email,
                    Registration.registration_form_id
                    == uncompleted_registration_form_id,
                ).one()
            except NoResultFound:
                data["payment_allowed"] = True
            except MultipleResultsFound:
                data["payment_allowed"] = False
                data["message"] = (
                    "Multiple registrations with the same email in the related registration found! Please contact the organizers to resolve the conflict. "
                )
                return
            else:
                if related_registration.state == RegistrationState.complete:
                    data["payment_allowed"] = False
                    data["message"] = (
                        "Related registration has been completed. This payment is not allowed. Please refer to the notices."
                    )
                    return
                else:
                    data["payment_allowed"] = True
        else:
            data["payment_allowed"] = True

        # -------- now the payment method is allowed --------

        # -------- pay logo url --------
        data["logo_url"] = url_for_plugin(
            self.name + ".static", filename="images/logo.png"
        )

        # -------- common fields --------
        data["paymentItemId"] = event_settings["payment_item_id"]
        data["method"] = "trade.pay.page"
        data["timestamp"] = requests.get(settings["url_time"]).text
        data["signType"] = "RSA"
        data["charset"] = "UTF-8"
        data["version"] = "3.0"

        # -------- biz content --------
        trade_name = f"{plain_name} payment for {registration.registration_form.title} of {plain_title}"
        if len(trade_name) > 64:
            trade_name = trade_name[:64]

        trade_summary = f"{plain_name} ({registration.email}) payment for {registration.registration_form.title} of {plain_title}"
        if len(trade_summary) > 127:
            trade_summary = trade_summary[:127]

        biz_content = {}
        biz_content["paymentChannel"] = ""
        biz_content["outTradeNo"] = str(time.time())
        biz_content["tradeName"] = trade_name
        biz_content["tradeAmount"] = round(amount, 2).to_eng_string()
        biz_content["moneyTypeId"] = currency
        biz_content["timeout"] = "15"
        biz_content["tradeSummary"] = trade_summary
        biz_content["payerId"] = ""
        biz_content["payerIdType"] = ""
        biz_content["payerName"] = registration.friendly_id
        biz_content["payerEmail"] = registration.email
        biz_content["returnUrl"] = url_for_plugin(
            "payment_thupay.success", registration.locator.uuid, _external=True
        )
        biz_content["notifyUrl"] = url_for_plugin(
            "payment_thupay.notify", registration.locator.uuid, _external=True
        )
        biz_content = dict(sorted(biz_content.items()))

        data["bizContent"] = json.dumps(biz_content)

        # -------- signing --------
        fields_to_submit = [
            "paymentItemId",
            "method",
            "timestamp",
            "signType",
            "charset",
            "version",
            "bizContent",
        ]
        data_to_sign = {key: data[key] for key in fields_to_submit}

        private_key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + event_settings["payment_item_key"]
            + "\n-----END RSA PRIVATE KEY-----"
        )
        rsa_util = RsaUtil(private_key=private_key)

        encrypt_str = RsaUtil.encrypt_str(data_to_sign)
        signature = rsa_util.create_sign(encrypt_str)
        data["sign"] = signature

        # -------- dealing with foreign card parameters --------
        data["method_fc"] = "trade.pay.page.fc"

        biz_content["paymentChannel"] = "boc.page.fc"
        data["bizContent_fc"] = json.dumps(biz_content)

        data_to_sign["method"] = data["method_fc"]
        data_to_sign["bizContent"] = data["bizContent_fc"]
        encrypt_str = rsa_util.encrypt_str(data_to_sign)
        signature = rsa_util.create_sign(encrypt_str)
        data["sign_fc"] = signature

        Logger.get().debug(encrypt_str)
