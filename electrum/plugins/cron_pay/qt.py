#!/usr/bin/env python
#
# Electrum - Lightweight Bitcoin Client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import json
import stat
import asyncio
import time

from decimal import Decimal

from datetime import datetime

from enum import IntEnum

from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QItemSelectionModel, pyqtSignal, QObject
from PyQt5.QtWidgets import QPushButton, QVBoxLayout, QLabel, QGridLayout, QLineEdit

from electrum import constants

from electrum.bitcoin import COIN

from electrum.i18n import _
from electrum.crypto import sha256
from electrum.gui.qt.util import Buttons, CloseButton, OkButton, HelpLabel, WindowModalDialog, MyTreeView, read_QIcon, TaskThread
from electrum.gui.qt.confirm_tx_dialog import ConfirmTxDialog
from electrum.transaction import TxOutput, PartialTxInput, PartialTxOutput, Transaction
from electrum.plugin import BasePlugin, hook
from electrum.util import log_exceptions, user_dir, bh2u, PR_TYPE_ONCHAIN, PR_TYPE_LN, PR_UNPAID, PR_PAID, PR_INFLIGHT, InvoiceError
from electrum.lnutil import PaymentFailure, SENT
from electrum.lnworker import PaymentInfo, NoPathFound
from electrum.lnaddr import lndecode
from electrum.network import TxBroadcastError, BestEffortRequestFailed
from electrum.paymentrequest import make_request


_CRON_PAY_SIGN_BEFORE_SCHEDULE = False



def read_schedule_file():
    schedule_path = os.path.join(user_dir(), 'schedule')
    if not os.path.exists(schedule_path):
        return {}    
    try:
        with open(schedule_path, "r", encoding='utf-8') as f:
            data = f.read()
        result = json.loads(data)
    except:
        return {}
    if not type(result) is dict:
        return {}
    return result


def write_schedule_file(schedule_dict):
    schedule_path = os.path.join(user_dir(), 'schedule')
    s = json.dumps(schedule_dict, indent=4, sort_keys=True)
    try:
        with open(schedule_path, "w", encoding='utf-8') as f:
            f.write(s)
        os.chmod(schedule_path, stat.S_IREAD | stat.S_IWRITE)
    except FileNotFoundError:
        if os.path.exists(schedule_path):
            raise


ROLE_REQUEST_TYPE = Qt.UserRole
ROLE_REQUEST_ID = Qt.UserRole + 1


class CronPaymentsList(MyTreeView):

    class Columns(IntEnum):
        ADDRESS = 0
        AMOUNT = 1
        DESCRIPTION = 2
        LAST_TIME = 3
        NEXT_TIME = 4

    headers = {
        Columns.ADDRESS: _('Address'),
        Columns.AMOUNT: _('Amount'),
        Columns.DESCRIPTION: _('Description'),
        Columns.LAST_TIME: _('Last payment'),
        Columns.NEXT_TIME: _('Next payment'),
    }
    filter_columns = [Columns.LAST_TIME, Columns.NEXT_TIME, Columns.AMOUNT]

    def __init__(self, parent):
        super().__init__(parent, lambda: None,
                         stretch_column=self.Columns.DESCRIPTION,
                         editable_columns=[])
        self.setMinimumSize(900, 200)
        self.setSortingEnabled(True)
        self.setModel(QStandardItemModel(self))
        self.update()

    def update(self):
        current_schedule = read_schedule_file()
        _list = current_schedule.get('items', [])
        self.model().clear()
        self.update_headers(self.__class__.headers)
        for idx, cron_item in enumerate(_list):
            invoice = cron_item['invoice']
            schedule = cron_item['schedule']
            invoice_type = invoice['type']
            if invoice_type == PR_TYPE_LN:
                key = invoice['rhash']
                icon_name = 'lightning.png'
                address_str = invoice['pubkey']
            elif invoice_type == PR_TYPE_ONCHAIN:
                key = bh2u(sha256(repr(invoice))[0:16])
                icon_name = 'bitcoin.png'
                if invoice.get('bip70'):
                    icon_name = 'seal.png'
                address_str = invoice['outputs'][0]['address']
            else:
                raise Exception('Incorrect payment item: %r' % cron_item)
            message = invoice['message']
            amount = invoice['amount']
            amount_str = self.parent.format_amount(amount, whitespaces=True)
            last_str = ''
            next_str = ''
            try:
                from croniter import croniter
                cron = '{minute} {hour} {day} {month} {week}'.format(**schedule)
                croniter_obj = croniter(cron, datetime.now())
                next_str = croniter_obj.get_next(datetime).isoformat()
            except:
                pass
            labels = [address_str, amount_str, message, last_str, next_str, ]
            items = [QStandardItem(e) for e in labels]
            self.set_editability(items)
            items[self.Columns.ADDRESS].setIcon(read_QIcon(icon_name))
            items[self.Columns.ADDRESS].setData(key, role=ROLE_REQUEST_ID)
            items[self.Columns.ADDRESS].setData(invoice_type, role=ROLE_REQUEST_TYPE)
            self.model().insertRow(idx, items)
        self.selectionModel().select(self.model().index(0,0), QItemSelectionModel.SelectCurrent)
        self.filter()


class CronPaymentsDialog(WindowModalDialog):

    def __init__(self, parent, plugin_instance, title=None):
        WindowModalDialog.__init__(self, parent, title=title)
        vbox = QVBoxLayout(self)
        self.plugin_instance = plugin_instance
        self.cron_list = CronPaymentsList(parent)
        btn_new_payment = QPushButton('New payment')
        btn_edit_schedule = QPushButton('Edit schedule')
        btn_cancel_schedule =  QPushButton('Cancel payment')
        btn_new_payment.clicked.connect(lambda: [parent.show_send_tab(), self.close(), ])
        btn_cancel_schedule.clicked.connect(lambda: self.on_cancel_button_clicked(parent))
        vbox.addWidget(self.cron_list)
        vbox.addStretch()
        vbox.addLayout(Buttons(
            btn_new_payment,
            btn_edit_schedule,
            btn_cancel_schedule,
        ))
        vbox.addStretch()
        vbox.addLayout(Buttons(CloseButton(self), ))

    def on_cancel_button_clicked(self, parent):
        cur_index = self.cron_list.currentIndex()
        if not cur_index.isValid():
            return
        cur_item_address = self.cron_list.model().item(cur_index.row(), 0).text()
        cur_item_amount = self.cron_list.model().item(cur_index.row(), 1).text()
        cur_item_message = self.cron_list.model().item(cur_index.row(), 2).text()
        index_parent = cur_index.parent()
        self.cron_list.selectionModel().setCurrentIndex(self.cron_list.indexAbove(cur_index), QItemSelectionModel.ClearAndSelect)
        self.cron_list.model().removeRow(cur_index.row(), parent=index_parent)
        current_schedule = read_schedule_file()
        if 'items' not in current_schedule:
            current_schedule['items'] = []
        for item in current_schedule['items']:
            inv = item['invoice']
            if inv['type'] == PR_TYPE_ONCHAIN:
                if parent.format_amount(inv['amount'], whitespaces=True) == cur_item_amount and inv['message'] == cur_item_message and inv['outputs'][0]['address'] == cur_item_address:
                    current_schedule['items'].remove(item)
                    break
            else:
                if parent.format_amount(inv['amount'], whitespaces=True) == cur_item_amount and inv['message'] == cur_item_message and inv['pubkey'] == cur_item_address:
                    current_schedule['items'].remove(item)
                    break
        write_schedule_file(current_schedule)
        self.plugin_instance.cancel_started_task(cur_item_address, cur_item_amount, cur_item_message)


class ScheduleDialog(WindowModalDialog):

    def __init__(self, main_win):
        WindowModalDialog.__init__(self, main_win, title=_("New scheduled payment"),)
        vbox = QVBoxLayout(self)
        self.setMinimumSize(400, 150)
        minute_label = HelpLabel(_('minute:'), _('''
Defines a specific minute for an hour.
Allowed characters:
"*" any value
"," value list separator
"-" range of values
"/" step values
0-59 allowed values
'''))
        hour_label = HelpLabel(_('hour:'), _('''
Defines a specific hour during the day.
Allowed characters:
"*" any value
"," value list separator
"-" range of values
"/" step values
0-23 allowed values
'''))
        day_label = HelpLabel(_('day of month:'), _('''
Defines the day number for the month.
Allowed characters:
"*" any value
"," value list separator
"-" range of values
"/" step values
0-31 allowed values
'''))
        month_label = HelpLabel(_('month:'), _('''
Defines a month during the year.
Allowed characters:
"*" any value
"," value list separator
"-" range of values
"/" step values
1-12 allowed values
'''))
        week_label = HelpLabel(_('day of week:'), _('''
Specifies the day of the week.
Allowed characters:
"*" any value
"," value list separator
"-" range of values
"/" step values
0-6 allowed values
'''))
        self.minute_edit = QLineEdit('*')
        self.minute_edit.setFixedWidth(30)
        self.minute_edit.textChanged.connect(self.update_schedule_details)
        self.hour_edit = QLineEdit('*')
        self.hour_edit.setFixedWidth(30)
        self.hour_edit.textChanged.connect(self.update_schedule_details)
        self.day_edit = QLineEdit('*')
        self.day_edit.setFixedWidth(30)
        self.day_edit.textChanged.connect(self.update_schedule_details)
        self.month_edit = QLineEdit('*')
        self.month_edit.setFixedWidth(30)
        self.month_edit.textChanged.connect(self.update_schedule_details)
        self.week_edit = QLineEdit('*')
        self.week_edit.setFixedWidth(30)
        self.week_edit.textChanged.connect(self.update_schedule_details)
        grid = QGridLayout()
        grid.addWidget(minute_label, 0, 0, alignment=Qt.AlignCenter)
        grid.addWidget(hour_label, 0, 1, alignment=Qt.AlignCenter)
        grid.addWidget(day_label, 0, 2, alignment=Qt.AlignCenter)
        grid.addWidget(month_label, 0, 3, alignment=Qt.AlignCenter)
        grid.addWidget(week_label, 0, 4, alignment=Qt.AlignCenter)
        grid.addWidget(self.minute_edit, 1, 0, alignment=Qt.AlignCenter)
        grid.addWidget(self.hour_edit, 1, 1, alignment=Qt.AlignCenter)
        grid.addWidget(self.day_edit, 1, 2, alignment=Qt.AlignCenter)
        grid.addWidget(self.month_edit, 1, 3, alignment=Qt.AlignCenter)
        grid.addWidget(self.week_edit, 1, 4, alignment=Qt.AlignCenter)
        every_day = QPushButton('every day')
        every_day.clicked.connect(lambda: self.set_as_text('0 0 * * *'))
        every_week = QPushButton('every week')
        every_week.clicked.connect(lambda: self.set_as_text('0 0 * * 0'))
        every_month = QPushButton('every month')
        every_month.clicked.connect(lambda: self.set_as_text('0 0 1 * *'))
        every_year = QPushButton('every year')
        every_year.clicked.connect(lambda: self.set_as_text('0 0 1 1 *'))
        self.schedule_details = QLabel('')
        vbox.addLayout(grid)
        vbox.addLayout(Buttons(every_day, every_week, every_month, every_year))
        vbox.addWidget(self.schedule_details)
        vbox.addStretch()
        vbox.addLayout(Buttons(CloseButton(self), OkButton(self)))
        self.update_schedule_details()

    def set_as_text(self, value):
        items = value.split(' ')
        self.minute_edit.setText(items[0])
        self.minute_edit.repaint()
        self.hour_edit.setText(items[1])
        self.hour_edit.repaint()
        self.day_edit.setText(items[2])
        self.day_edit.repaint()
        self.month_edit.setText(items[3])
        self.month_edit.repaint()
        self.week_edit.setText(items[4])
        self.week_edit.repaint()
        self.update_schedule_details()

    def update_schedule_details(self):
        try:
            from croniter import croniter
            item = croniter(self.as_text(), datetime.now())
            t = item.get_next(datetime)
        except:
            self.schedule_details.setText('')
            return
        self.schedule_details.setText('new payment expected on:\n%s' % t.strftime("%A, %d %B %Y at %I:%M%p"))

    def as_text(self):
        return '%s %s %s %s %s' % (
            self.minute_edit.text(),
            self.hour_edit.text(),
            self.day_edit.text(),
            self.month_edit.text(),
            self.week_edit.text(),
        )

    def as_dict(self):
        return {
            'minute': self.minute_edit.text(),
            'hour': self.hour_edit.text(),
            'day': self.day_edit.text(),
            'month': self.month_edit.text(),
            'week': self.week_edit.text(),
        }


class ScheduleButton(QPushButton):

    def __init__(self, main_window, plugin_instance):
        self.plugin_instance = plugin_instance
        QPushButton.__init__(self, _("Schedule"))
        self.clicked.connect(lambda: self._on_clicked(main_window))

    def _on_clicked(self, main_win):
        global _CRON_PAY_SIGN_BEFORE_SCHEDULE
        outputs = main_win.read_outputs()
        print('ScheduleButton._on_clicked', outputs)
        if main_win.is_onchain and main_win.check_send_tab_onchain_outputs_and_show_errors(outputs):
            return
        invoice = main_win.read_invoice()
        if not invoice:
            return
        future_invoice = {}
        if invoice['type'] == PR_TYPE_ONCHAIN:
            # translate `TxOutput` list to json list
            outputs = []
            for output in invoice['outputs']:
                if isinstance(output, TxOutput):
                    output = output.to_json()
                outputs.append(output)
            future_invoice.update(invoice)
            future_invoice['outputs'] = outputs
        else:
            future_invoice.update(invoice)
        current_schedule = read_schedule_file()
        if 'items' not in current_schedule:
            current_schedule['items'] = []
        schedule_dialog = ScheduleDialog(main_win)
        schedule_dialog.show()
        if not schedule_dialog.exec_():
            return
        if future_invoice['type'] == PR_TYPE_ONCHAIN:
            outputs = invoice['outputs']
            make_tx = lambda fee_est: main_win.wallet.make_unsigned_transaction(
                coins=main_win.get_coins(),
                outputs=outputs,
                fee=fee_est,
                is_sweep=False)
            output_values = [x.value for x in outputs]
            if output_values.count('!') > 1:
                self.show_error(_("More than one output set to spend max"))
                return
            output_value = '!' if '!' in output_values else sum(output_values)
            confirm_dialog = ConfirmTxDialog(window=main_win, make_tx=make_tx, output_value=output_value, is_sweep=False)
            confirm_dialog.send_button.setText(_('Start'))
            confirm_dialog.update_tx()
            if confirm_dialog.not_enough_funds:
                main_win.show_message(_('Not Enough Funds'))
                return
            cancelled, is_send, password, tx = confirm_dialog.run()
            if cancelled:
                return
            if not is_send:
                return

            new_item = {
                'invoice': future_invoice,
                'schedule': schedule_dialog.as_dict(),
            }

#             if _CRON_PAY_SIGN_BEFORE_SCHEDULE:
#                 def sign_done(success):
#                     if success:
#                         new_item['invoice']['transaction'] = tx.serialize()
#                         current_schedule['items'].append(new_item)
#                         write_schedule_file(current_schedule)
#                         main_win.do_clear()
#                         main_win.show_message(_('New automatic payment signed and scheduled successfully'))
#                         self.plugin_instance.start_scheduler()
#                         return
# 
#                 main_win.sign_tx_with_password(tx, callback=sign_done, password=password, external_keypairs=None)
#             else:
            current_schedule['items'].append(new_item)
            write_schedule_file(current_schedule)
            main_win.do_clear()
            main_win.show_message(_('New automatic payment scheduled successfully'))
            self.plugin_instance.start_scheduler()

        else:
            new_item = {
                'invoice': future_invoice,
                'schedule': schedule_dialog.as_dict(),
            }
            current_schedule['items'].append(new_item)
            write_schedule_file(current_schedule)
            main_win.do_clear()
            main_win.show_message(_('New automatic payment scheduled successfully'))
            self.plugin_instance.start_scheduler()


class Plugin(BasePlugin, QObject):

    scheduled_payment_signal = pyqtSignal(dict)

    def fullname(self):
        return 'Cron Payments'

    def description(self):
        return _("Automated scheduled BitCoin payments")

    def is_available(self):
        return True

    def __init__(self, parent, config, name):
        self.scheduled_tasks = []
        self.main_window = None
        BasePlugin.__init__(self, parent, config, name)
        QObject.__init__(self)
        self.start_scheduler()

    def on_cron_payments_menu_clicked(self, window, tools_menu):
        d = CronPaymentsDialog(window, self, _("Scheduled payments"))
        d.show()

    @hook
    def init_menubar_tools(self, main_window, tools_menu):
        self.main_window = main_window
        self.scheduled_payment_signal.connect(self.on_scheduled_payment)
        tools_menu.addSeparator()
        tools_menu.addAction(_("&Scheduled payments"), lambda: self.on_cron_payments_menu_clicked(main_window, tools_menu))
        buttons = main_window.send_grid.itemAtPosition(6, 1)
        schedule_button = ScheduleButton(main_window, self)
        buttons.addWidget(schedule_button)

    @log_exceptions
    async def ln_pay_proc(self, invoice, amount_sat=None, attempts=1):
        lnaddr = lndecode(invoice, expected_hrp=constants.net.SEGWIT_HRP)
        if amount_sat:
            lnaddr.amount = Decimal(amount_sat) / COIN
        if lnaddr.amount is None:
            raise InvoiceError(_("Missing amount"))
        if lnaddr.get_min_final_cltv_expiry() > 60 * 144:
            raise InvoiceError("{}\n{}".format(
                _("Invoice wants us to risk locking funds for unreasonably long."),
                f"min_final_cltv_expiry: {lnaddr.get_min_final_cltv_expiry()}"))
        payment_hash = lnaddr.paymenthash
        key = payment_hash.hex()
        amount = int(lnaddr.amount * COIN)
        status = self.main_window.wallet.lnworker.get_payment_status(payment_hash)
        if status == PR_PAID:
            raise PaymentFailure(_("This invoice has been paid already"))
        if status == PR_INFLIGHT:
            raise PaymentFailure(_("A payment was already initiated for this invoice"))
        info = PaymentInfo(lnaddr.paymenthash, amount, SENT, PR_UNPAID)
        self.main_window.wallet.lnworker.save_payment_info(info)
        self.main_window.wallet.lnworker.wallet.set_label(key, lnaddr.get_description())
        log = self.main_window.wallet.lnworker.logs[key]
        for i in range(attempts):
            try:
                route = await self.main_window.wallet.lnworker._create_route_from_invoice(decoded_invoice=lnaddr)
            except NoPathFound:
                success = False
                break
            success, preimage, failure_log = await self.main_window.wallet.lnworker._pay_to_route(route, lnaddr)
            if success:
                log.append((route, True, preimage))
                break
            else:
                log.append((route, False, failure_log))
        return success

    def ln_pay_run(self, invoice, amount_sat=None, attempts=1):
        coro = self.ln_pay_proc(invoice, amount_sat, attempts)
        fut = asyncio.run_coroutine_threadsafe(coro, self.main_window.wallet.lnworker.network.asyncio_loop)
        success = fut.result()
        return success

    def do_pay_ln(self, invoice):
        def ln_pay_task():
            print('ln_pay_task', invoice['invoice'], invoice['amount'])
            self.ln_pay_run(invoice['invoice'], amount_sat=invoice['amount'], attempts=1)
            print('ln_pay_task done')
        self.main_window.wallet.thread.add(ln_pay_task)

    def do_broadcast_transaction(self, tx: Transaction):
        # broadcasting_thread = TaskThread(self)

        def broadcast_thread():
            try:
                self.main_window.network.run_from_another_thread(self.main_window.network.broadcast_transaction(tx))
            except TxBroadcastError as e:
                print(e)
                return False, e.get_message_for_gui()
            except BestEffortRequestFailed as e:
                print(e)
                return False, repr(e)
            # success
            txid = tx.txid()
            print('broadcast_thread OK', txid)
            return True, txid

#         def broadcast_done(result):
#             print('broadcast_done', result)
#             # GUI thread
#             if result:
#                 success, msg = result
#                 if success:
#                     # parent.show_message(_('Payment sent.') + '\n' + msg)
#                     self.invoice_list.update()
#                     self.do_clear()
#                 else:
#                     msg = msg or ''
#                     # parent.show_error(msg)

#         def thread_finished(x):
#             print('thread_finished', x)
#             # broadcasting_thread.stop()

#         def thread_error(self, exc_info):
#             print('thread_error', exc_info)
        
        # broadcasting_thread.add(broadcast_thread, broadcast_done, thread_finished, thread_error)
        # broadcasting_thread.wait()
        
        broadcast_thread()

    def do_pay_on_chain(self, invoice):
        print('do_pay_on_chain', invoice)
        tx = self.main_window.tx_from_text(invoice['transaction'])
        print('do_pay_on_chain tx:', tx)
        try:
            self.do_broadcast_transaction(tx)
        except Exception as e:
            print('failed to broadcast: ', e)
        print('do_pay_on_chain broadcast_transaction OK')

    def is_task_started(self, cron_item):
        for t, i in self.scheduled_tasks:
            if i == cron_item:
                return True
        return False

    def start_scheduler(self):
        try:
            from croniter import croniter
        except:
            return False
        loop = asyncio.get_event_loop()
        current_schedule = read_schedule_file()
        print('start_scheduler current_schedule :', current_schedule)
        _list = current_schedule.get('items', [])
        for idx, cron_item in enumerate(_list):
            if self.is_task_started(cron_item):
                continue
            schedule = cron_item['schedule']
            cron = '{minute} {hour} {day} {month} {week}'.format(**schedule)
            item = croniter(cron, datetime.now())
            next_time = item.get_next(datetime)
            time_delta = next_time - datetime.now()
            transaction_delay = time_delta.seconds
            # if True:
            #     transaction_delay = 10
            new_task = loop.call_later(transaction_delay, self.trigger_payment, cron_item)
            self.scheduled_tasks.append((new_task, cron_item, ))
            print('start_scheduler', new_task)
        return True

    def cancel_started_task(self, address, amount, message):
        for running_task, cron_item in self.scheduled_tasks:
            inv = cron_item['invoice']
            if ['type'] == PR_TYPE_ONCHAIN:
                if self.main_window.format_amount(inv['amount']) == amount and inv['outputs'][0]['address'] == address and inv['message'] == message:
                    self.scheduled_tasks.remove((running_task, cron_item, ))
                    running_task.cancel()
                    print('cancel_started_task', cron_item)
                    break
            else:
                if self.main_window.format_amount(inv['amount']) == amount and inv['pubkey'] == address and inv['message'] == message:
                    self.scheduled_tasks.remove((running_task, cron_item, ))
                    running_task.cancel()
                    print('cancel_started_task', cron_item)
                    break

    def schedule_next_task(self, current_item):
        try:
            from croniter import croniter
        except:
            return False
        loop = asyncio.get_event_loop()
        current_schedule = read_schedule_file()
        _list = current_schedule.get('items', [])
        curinv = current_item['invoice']
        print('schedule_next_task', current_item, len(_list))
        for idx, cron_item in enumerate(_list):
            if self.is_task_started(cron_item):
                print('schedule_next_task already started', cron_item)
                continue
            inv = cron_item['invoice']
            if inv['type'] == curinv['type'] and inv['amount'] == curinv['amount'] and inv['message'] == curinv['message']:
                if inv['type'] == PR_TYPE_ONCHAIN and inv.get('outputs', []) and curinv.get('outputs', []):
                    if inv['outputs'][0]['address'] == curinv['outputs'][0]['address']:
                        schedule = cron_item['schedule']
                        cron = '{minute} {hour} {day} {month} {week}'.format(**schedule)
                        item = croniter(cron, datetime.now())
                        next_time = item.get_next(datetime)
                        time_delta = next_time - datetime.now()
                        new_task = loop.call_later(time_delta.seconds, self.trigger_payment, cron_item)
                        self.scheduled_tasks.append((new_task, cron_item, ))
                        print('schedule_next_task OK', new_task)
                        
                elif inv['type'] == PR_TYPE_LN:
                    if inv['rhash'] == curinv['rhash'] and inv['pubkey'] == curinv['pubkey'] and inv['amount'] == curinv['amount']:
                        schedule = cron_item['schedule']
                        cron = '{minute} {hour} {day} {month} {week}'.format(**schedule)
                        item = croniter(cron, datetime.now())
                        next_time = item.get_next(datetime)
                        time_delta = next_time - datetime.now()
                        new_task = loop.call_later(time_delta.seconds, self.trigger_payment, cron_item)
                        self.scheduled_tasks.append((new_task, cron_item, ))
                        print('schedule_next_task OK', new_task)
                    
                else:
                    raise Exception('unknown cron item type')

    def trigger_payment(self, cron_item):
        print('trigger_payment', cron_item)
        self.scheduled_payment_signal.emit(cron_item)

    def on_scheduled_payment(self, cron_item):
        print('on_scheduled_payment', cron_item)
        self.execute_payment(cron_item)

    def execute_payment(self, cron_item):
        print('execute_payment', cron_item, self.main_window)
        if not self.main_window:
            print('failed to pay, no main_window known')
            return

        for tsk in self.scheduled_tasks:
            if tsk[1] == cron_item:
                self.scheduled_tasks.remove(tsk)
                print('clean up triggered task instance')
                break

        invoice = cron_item['invoice']

        if invoice['type'] == PR_TYPE_LN:
            self.do_pay_ln(invoice)

        elif invoice['type'] == PR_TYPE_ONCHAIN:

            coins = self.main_window.wallet.get_spendable_coins(None, nonlocal_only=False)
            outputs = []
    
            for o in invoice.get('outputs', []):
                # txout = TxOutput(value=int(o['value_sats']), scriptpubkey=o['scriptpubkey'].encode('utf-8'))
                txout = TxOutput.from_address_and_value(address=o['address'], value=int(o['value_sats']))
                p_txout = PartialTxOutput.from_txout(txout)
                outputs.append(p_txout)
                print('output:', o, txout, p_txout)
    
            print('coins', coins)
            print('outputs', outputs)
    
            tx = self.main_window.wallet.make_unsigned_transaction(
                coins=coins,
                outputs=outputs,
                fee=None,
                is_sweep=False,
            )

            signed_tx = self.main_window.wallet.sign_transaction(tx, password=None)
            invoice['transaction'] = signed_tx.serialize()
            print('execute_payment', invoice['transaction'])
            
            self.do_pay_on_chain(invoice)
        else:
            raise Exception('unknown invoice type')

        time.sleep(0.01)
        self.schedule_next_task(cron_item)
