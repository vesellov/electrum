from electrum.i18n import _

fullname = _('Cron Payments')
description = ' '.join([
    _("Automated scheduled BitCoin payments:\n"),
    _('+ create a "cron task" and define a time schedule for your transaction\n'),
    _("+ scheduler will automatically send certain amount to given recipient\n"),
    _("+ make sure you keep Electrum running so scheduler actually works\n"),
])
requires = [('croniter', 'https://github.com/taichino/croniter/')]
available_for = ['qt']
