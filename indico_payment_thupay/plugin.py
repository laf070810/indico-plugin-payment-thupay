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
from indico.util.string import remove_accents, str_to_ascii
from indico.web.forms.validators import UsedIf
from wtforms.fields import StringField, URLField
from wtforms.validators import DataRequired, Optional

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
        plain_name = str_to_ascii(remove_accents(registration.full_name))
        plain_title = str_to_ascii(remove_accents(event.title))
        amount = data["amount"]
        currency = data["currency"]

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
        biz_content = {}
        biz_content["outTradeNo"] = str(time.time())
        biz_content["tradeName"] = f"{plain_name}: registration for {plain_title}"
        biz_content["tradeAmount"] = round(amount, 2).to_eng_string()
        biz_content["moneyTypeId"] = currency
        biz_content["timeout"] = "15"
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
        encrypt_str = rsa_util.encrypt_str(data_to_sign)
        signature = rsa_util.create_sign(encrypt_str)
        data["sign"] = signature

        Logger.get().debug(encrypt_str)
