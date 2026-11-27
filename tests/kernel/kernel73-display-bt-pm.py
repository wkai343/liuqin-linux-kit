#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Exercise the Liuqin Bluetooth suspend handoff and DPU clock vote before resume."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kernel_test import function, run_c, write_report


def bluetooth(source):
    serial = (source / 'drivers/tty/serial/qcom_geni_serial.c').read_text()
    core = (source / 'drivers/tty/serial/serial_core.c').read_text()
    suspend = function(core, 'uart_suspend_port')
    assert suspend.index('enable_irq_wake(') < suspend.index('uport->suspended = 1') < suspend.index('ops->shutdown(')
    assert 'IRQ_NOAUTOEN' in function(serial, 'qcom_geni_serial_probe')
    startup = function(serial, 'qcom_geni_serial_startup')
    assert startup.index('qcom_geni_serial_start_rx(') < startup.index('enable_irq(')
    shutdown = function(serial, 'qcom_geni_serial_shutdown')
    assert shutdown.index('disable_irq(') < shutdown.index('pm_runtime_put_sync(')
    identify = function(serial, 'qcom_geni_serial_is_liuqin_bt')
    assert 'liuqin_board_quirks()' in identify and '"qcom,wcn6855-bt"' in identify
    assert '!data->console && qcom_geni_serial_is_liuqin_bt(' in function(serial, 'qcom_geni_serial_probe')

    stubs = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
struct qcom_geni_private_data { void *drv; };
struct uart_port { void *private_data; int irq; bool suspended; };
struct qcom_geni_serial_port { struct uart_port uport; bool guard_suspend_irq; int se; };
struct device { struct qcom_geni_serial_port port; };
#define dev_get_drvdata(d) (&(d)->port)
#define uart_console(p) (console)
#define geni_icc_set_tag(...) ((void)0)
#define geni_icc_set_bw(...) ((void)0)
static bool console, console_suspend_enabled, opened, wake, guarded;
static int depth, suspend_error, force_error, resume_error, startup_error, forced;
static void disable_irq(int irq) { assert(irq == 17); depth++; }
static void enable_irq(int irq) { assert(irq == 17 && depth > 0); depth--; }
static int uart_suspend_port(void *drv, struct uart_port *p) {
    if (guarded) assert(depth == 1 + !opened);
    if (suspend_error) return suspend_error;
    if (wake || (console && !console_suspend_enabled)) return 0;
    p->suspended = true;
    if (opened) disable_irq(p->irq);
    return 0;
}
static int uart_resume_port(void *drv, struct uart_port *p) {
    if (p->suspended && opened && !startup_error) enable_irq(p->irq);
    p->suspended = false;
    return 0;
}
static int pm_runtime_force_suspend(struct device *d) { forced++; return force_error; }
static int pm_runtime_force_resume(struct device *d) { forced--; return resume_error; }
'''
    actual = '\n'.join(function(serial, name) for name in (
        'qcom_geni_serial_keep_active', 'qcom_geni_serial_suspend', 'qcom_geni_serial_resume'))
    run_c(stubs + actual + r'''
int main(void) {
    struct qcom_geni_private_data data = {0};
    struct device dev = {.port = {.uport = {.private_data = &data, .irq = 17}}};
    struct qcom_geni_serial_port *port = &dev.port;
    for (int scope = 0; scope < 3; scope++) {
        guarded = port->guard_suspend_irq = scope == 0;
        console = scope == 2;
        for (int state = 0; state < 8; state++) {
            opened = state & 1; wake = state & 2; console_suspend_enabled = state & 4;
            depth = !opened; forced = 0;
            assert(!qcom_geni_serial_suspend(&dev));
            assert(depth == !opened + (opened && port->uport.suspended));
            assert(forced == !((guarded && wake) || (console && !console_suspend_enabled)));
            assert(!qcom_geni_serial_resume(&dev) && !port->uport.suspended);
            assert(depth == !opened && !forced);
        }
    }
    guarded = port->guard_suspend_irq = opened = true;
    console = wake = false; depth = forced = 0;
    suspend_error = -EBUSY;
    assert(qcom_geni_serial_suspend(&dev) == -EBUSY && !depth && !forced);
    suspend_error = 0; force_error = -EIO;
    assert(qcom_geni_serial_suspend(&dev) == -EIO && !depth && !port->uport.suspended);
    force_error = forced = 0; resume_error = -EIO;
    assert(!qcom_geni_serial_suspend(&dev) && depth == 1);
    assert(qcom_geni_serial_resume(&dev) == -EIO && port->uport.suspended && depth == 1);
    /* A separate successful suspend whose RX startup then fails. */
    port->uport.suspended = false; depth = forced = resume_error = 0;
    assert(!qcom_geni_serial_suspend(&dev) && depth == 1);
    startup_error = -EIO;
    assert(!qcom_geni_serial_resume(&dev) && depth == 1);
}
''', flags=('-Wno-unused-parameter',))


def hci_suspend(source):
    core = (source / 'net/bluetooth/hci_core.c').read_text()
    qca = (source / 'drivers/bluetooth/hci_qca.c').read_text()
    setup = function(qca, 'qca_setup').split('case QCA_WCN6855:', 1)[1].split('break;', 1)[0]
    assert 'if (liuqin_board_quirks())' in setup and 'HCI_QUIRK_WAIT_BEFORE_SUSPEND' in setup
    suspend = function(core, 'hci_suspend_dev')
    assert suspend.index('hci_suspend_req_lock(') < suspend.index('hci_suspend_sync(') < suspend.index('hci_req_sync_unlock(')
    # Model the completion and timeout winning on either side of mutex acquisition.
    stubs = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
struct work_struct { void (*run)(struct work_struct *); };
struct delayed_work { struct work_struct work; };
struct hci_dev { bool quirk, req_lock; };
struct hci_suspend_timeout { struct delayed_work work; struct hci_dev *hdev; };
#define container_of(ptr, type, member) ((type *)((char *)(ptr) - offsetof(type, member)))
#define to_delayed_work(w) container_of(w, struct delayed_work, work)
#define hci_test_quirk(dev, flag) ((dev)->quirk)
#define SUSPEND_NOTIFIER_TIMEOUT 2000
#define INIT_DELAYED_WORK_ONSTACK(d, fn) ((d)->work.run = (fn))
enum owner { IDLE, RESPONDS, STALLED };
static enum owner owner;
static struct delayed_work *armed;
static int canceled, scheduled, destroyed;
static bool mutex_trylock(bool *lock) {
    if (*lock) return false;
    return *lock = true;
}
static void hci_cancel_cmd_sync(struct hci_dev *dev, int err) {
    assert(err == EHOSTDOWN);
    canceled++;
    dev->req_lock = false;
}
static void schedule_delayed_work(struct delayed_work *w, int delay) {
    assert(!armed && delay == SUSPEND_NOTIFIER_TIMEOUT);
    armed = w;
    scheduled++;
}
static void hci_req_sync_lock(struct hci_dev *dev) {
    if (dev->req_lock && owner == RESPONDS)
        dev->req_lock = false;
    if (dev->req_lock && owner == STALLED) {
        assert(armed);
        armed->work.run(&armed->work);
    }
    assert(!dev->req_lock);
    dev->req_lock = true;
}
static void cancel_delayed_work_sync(struct delayed_work *w) {
    assert(w == armed);
    armed = NULL;
}
static void destroy_delayed_work_on_stack(struct delayed_work *w) {
    assert(!armed && w->work.run);
    destroyed++;
}
'''
    actual = '\n'.join(function(core, name) for name in ('hci_suspend_cancel_work', 'hci_suspend_req_lock'))
    run_c(stubs + actual + r'''
int main(void) {
    for (int quirk = 0; quirk <= 1; quirk++)
        for (owner = IDLE; owner <= STALLED; owner++) {
            struct hci_dev dev = { .quirk = quirk, .req_lock = owner != IDLE };
            canceled = scheduled = destroyed = 0;
            hci_suspend_req_lock(&dev);
            assert(dev.req_lock && !armed);
            assert(canceled == (!quirk || owner == STALLED));
            assert(scheduled == (quirk && owner != IDLE) && scheduled == destroyed);
        }
}
''')


def ibs_power_cycle(source):
    qca = (source / 'drivers/bluetooth/hci_qca.c').read_text()
    off = function(qca, 'qca_power_off')
    guard = 'if (hu->serdev && soc_type == QCA_WCN6855 && liuqin_board_quirks())'
    assert off.index(guard) < off.index('qca_stop_ibs(hu)') < off.index('pwrseq_disable(')
    assert 'qca_stop_ibs' not in function(qca, 'qca_suspend')
    on = function(qca, 'qca_power_on')
    assert on.index('qca_regulator_init(') < on.index('if (!ret)\n\t\tclear_bit(QCA_IBS_STOPPED')
    setup = function(qca, 'qca_setup')
    assert setup.index('set_bit(QCA_IBS_DISABLED') < setup.index('qca_power_on(') < setup.index('qca_uart_setup(')

    stubs = r'''
#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <stddef.h>
enum { QCA_IBS_STOPPED, QCA_IBS_DISABLED, QCA_SUSPENDING, QCA_SSR_TRIGGERED };
enum { HCI_IBS_TX_ASLEEP, HCI_IBS_TX_WAKING, HCI_IBS_TX_AWAKE };
enum { HCI_IBS_RX_ASLEEP, HCI_IBS_RX_AWAKE };
enum { HCI_IBS_WAKE_ACK, HCI_IBS_TX_VOTE_CLOCK_OFF, HCI_IBS_RX_VOTE_CLOCK_OFF };
struct sk_buff { char type, head; };
struct qca_data {
    unsigned long flags;
    int hci_ibs_lock, tx_ibs_state, rx_ibs_state, txq, tx_wait_q;
    int ws_awake_device, ws_awake_rx, ws_tx_vote_off, ws_rx_vote_off;
    int wake_retrans_timer, tx_idle_timer, tx_idle_delay;
    int ibs_recv_wakes, ibs_recv_slps, ibs_recv_wacks, ibs_sent_wacks;
    bool tx_vote, rx_vote;
};
struct hci_uart { struct qca_data *priv; };
static struct qca_data q;
static struct hci_uart hu = {&q};
static struct sk_buff packet;
static int freed, uart_kicks;
#define BT_DBG(...) ((void)0)
#define BT_ERR(...) assert(0)
#define bt_dev_dbg(...) ((void)0)
#define spin_lock_irqsave(lock, flags) do { assert(!*(lock)); *(lock)=1; (flags)=0; } while (0)
#define spin_unlock_irqrestore(lock, flags) do { assert(*(lock) && !(flags)); *(lock)=0; } while (0)
#define set_bit(bit, f) (*(f) |= 1UL << (bit))
#define test_bit(bit, f) (*(f) & (1UL << (bit)))
#define msecs_to_jiffies(n) (n)
#define jiffies 10
#define mod_timer(t, deadline) (*(t) = (deadline))
#define timer_delete(t) (*(t) = 0)
#define queue_work(wq, w) ((void)(*(w) = 1))
#define wake_up_interruptible(wq) ((void)0)
#define hci_uart_tx_wakeup(hu) ((void)uart_kicks++)
#define send_hci_ibs_cmd(cmd, hu) ((hu)->priv->txq++, 0)
#define skb_push(skb, n) (&(skb)->head)
#define hci_skb_pkt_type(skb) ((skb)->type)
#define kfree_skb(skb) ((void)freed++)
#define dev_kfree_skb_irq(skb) kfree_skb(skb)
#define skb_queue_purge(queue) (*(queue) = 0)
#define skb_queue_tail(queue, skb) ((void)(*(queue) += 1))
static struct sk_buff *skb_dequeue(int *queue) {
    if (!*queue) return NULL;
    (*queue)--;
    return &packet;
}
static void serial_clock_vote(int vote, struct hci_uart *uart) {
    assert(uart->priv->hci_ibs_lock);
    if (vote == HCI_IBS_TX_VOTE_CLOCK_OFF) { assert(q.tx_vote); q.tx_vote=false; }
    else { assert(vote == HCI_IBS_RX_VOTE_CLOCK_OFF && q.rx_vote); q.rx_vote=false; }
}
static void cancel_work_sync(int *work);
static void timer_delete_sync(int *timer);
'''
    actual = '\n'.join(function(qca, name) for name in (
        'qca_flush', 'device_want_to_wakeup', 'device_want_to_sleep',
        'device_woke_up', 'qca_enqueue', 'qca_stop_ibs'))
    run_c(stubs + actual + r'''
/* Deliver UART indications as the synchronous primitives drop the IBS lock. */
static void late_traffic(void) {
    assert(!q.hci_ibs_lock && test_bit(QCA_IBS_STOPPED, &q.flags));
    device_want_to_wakeup(&hu);
    device_want_to_sleep(&hu);
    device_woke_up(&hu);
    qca_enqueue(&hu, &packet);
    assert(!q.ibs_recv_wakes && !q.ibs_recv_slps && !q.ibs_recv_wacks);
}
static void cancel_work_sync(int *work) {
    late_traffic();
    if (*work == 1 && work == &q.ws_awake_device) q.wake_retrans_timer = 1;
    if (*work == 1 && work == &q.ws_awake_rx) q.rx_vote = true;
    *work = 0;
}
static void timer_delete_sync(int *timer) {
    late_traffic();
    if (*timer && timer == &q.tx_idle_timer) q.ws_tx_vote_off = 1;
    *timer = 0;
}
int main(void) {
    for (int tx = HCI_IBS_TX_ASLEEP; tx <= HCI_IBS_TX_AWAKE; tx++) {
        for (int rx = HCI_IBS_RX_ASLEEP; rx <= HCI_IBS_RX_AWAKE; rx++) {
            q = (struct qca_data){.tx_ibs_state=tx, .rx_ibs_state=rx, .tx_vote=true,
                .txq=1, .tx_wait_q=2, .ws_awake_device=1, .ws_awake_rx=1,
                .ws_rx_vote_off=1, .tx_idle_timer=1, .tx_idle_delay=2000};
            freed = uart_kicks = 0;
            qca_stop_ibs(&hu);
            assert(!q.hci_ibs_lock && !q.txq && !q.tx_wait_q && !q.tx_vote && !q.rx_vote);
            assert(q.tx_ibs_state == HCI_IBS_TX_ASLEEP && q.rx_ibs_state == HCI_IBS_RX_ASLEEP);
            assert(!q.ws_awake_device && !q.ws_awake_rx && !q.ws_tx_vote_off && !q.ws_rx_vote_off);
            assert(!q.wake_retrans_timer && !q.tx_idle_timer && freed > 0 && !uart_kicks);
            /* Setup uses raw HCI. The first regular command must wake and wait for ACK. */
            q.flags &= ~(1UL << QCA_IBS_STOPPED);
            qca_enqueue(&hu, &packet);
            assert(q.txq == 1 && !q.tx_wait_q && !q.ws_awake_device);
            q.flags &= ~(1UL << QCA_IBS_DISABLED);
            qca_enqueue(&hu, &packet);
            assert(q.tx_ibs_state == HCI_IBS_TX_WAKING && q.tx_wait_q == 1 && q.ws_awake_device);
            device_woke_up(&hu);
            assert(q.tx_ibs_state == HCI_IBS_TX_AWAKE && !q.tx_wait_q && q.txq == 2 && q.tx_idle_timer);
            device_want_to_wakeup(&hu);
            assert(q.ws_awake_rx && q.ibs_recv_wakes == 1);
        }
    }
}
''', flags=('-Wno-unused-parameter',))


def display(source):
    directory = source / 'drivers/gpu/drm/msm/disp/dpu1'
    kms = (directory / 'dpu_kms.c').read_text()
    perf = function((directory / 'dpu_core_perf.c').read_text(), 'dpu_core_perf_crtc_update')
    assert perf.index('if (!clk_rate)\n\t\t\treturn 0;') < perf.index('dev_pm_opp_set_rate(') < perf.index('kms->perf.core_clk_rate = clk_rate')
    probe = function(kms, 'dpu_dev_probe')
    assert probe.index('liuqin_board_quirks()') < probe.index('dev_pm_opp_find_freq_floor(') < probe.index('if (IS_ERR(opp))') < probe.index('initial_core_opp_rate = rate') < probe.index('msm_drv_probe(')
    hw_init = function(kms, 'dpu_kms_hw_init')
    first_resume = hw_init.split('pm_runtime_resume_and_get', 1)[1].split('readl_relaxed', 1)[0]
    assert 'if (rc < 0)' in first_resume and 'return rc;' in first_resume
    stubs = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#define __maybe_unused
struct device {};
struct drm_device { struct device *dev; void *dev_private; };
struct dpu_kms {
    struct drm_device *dev; struct { unsigned long core_clk_rate; } perf;
    unsigned long initial_core_opp_rate; bool rpm_enabled;
    int base, num_clocks, clocks;
};
struct msm_drm_private { struct dpu_kms *kms; };
struct platform_device { struct device dev; struct msm_drm_private priv; };
struct drm_encoder {}; struct dev_pm_opp {};
#define to_platform_device(d) ((struct platform_device *)(d))
#define platform_get_drvdata(p) (&(p)->priv)
#define to_dpu_kms(k) (k)
#define IS_ERR(p) ((intptr_t)(p) < 0)
#define dev_pm_opp_put(p) ((void)(p))
#define msm_kms_init(...) (0)
#define pm_runtime_enable(d) ((void)(d))
#define drm_for_each_encoder(e, d) for ((e) = 0; (e); )
#define dpu_encoder_virt_runtime_resume(e) ((void)(e))
#define DPU_ERROR(...) ((void)0)
#define dev_err(...) ((void)0)
static unsigned long voted;
static int events, vote_error, clk_error;
static struct dev_pm_opp *dev_pm_opp_find_freq_floor(struct device *dev, unsigned long *freq) {
    assert(*freq == ULONG_MAX);
    return (struct dev_pm_opp *)(intptr_t)-ENOENT;
}
static int dev_pm_opp_set_rate(struct device *dev, unsigned long rate) {
    events = 10 * events + 1; voted = rate; return vote_error;
}
static int clk_bulk_prepare_enable(int n, int clks) { events = 10 * events + 2; return clk_error; }
static void dpu_vbif_init_memtypes(struct dpu_kms *kms) { events = 10 * events + 3; }
'''
    actual = function(kms, 'dpu_kms_init') + '\n' + function(kms, 'dpu_runtime_resume')
    run_c(stubs + actual + r'''
int main(void) {
    struct dpu_kms kms = {.initial_core_opp_rate = 500};
    struct platform_device p = {.priv = {.kms = &kms}};
    struct drm_device drm = {.dev = &p.dev, .dev_private = &p.priv};
    assert(!dpu_kms_init(&drm) && kms.rpm_enabled && !events);
    vote_error = -EIO;
    assert(dpu_runtime_resume(&p.dev) == -EIO && events == 1 && voted == 500);
    events = vote_error = 0;
    assert(!dpu_runtime_resume(&p.dev) && events == 123 && voted == 500);
    kms.perf.core_clk_rate = 325;
    events = 0; vote_error = -EIO;
    assert(dpu_runtime_resume(&p.dev) == -EIO && events == 1 && voted == 325);
    events = vote_error = 0; clk_error = -EBUSY;
    assert(dpu_runtime_resume(&p.dev) == -EBUSY && events == 12);
    events = clk_error = 0;
    assert(!dpu_runtime_resume(&p.dev) && events == 123 && voted == 325);
    events = 0;
    assert(!dpu_kms_init(&drm) && kms.perf.core_clk_rate == 500);
    kms.initial_core_opp_rate = 0; vote_error = -EINVAL;
    assert(!dpu_kms_init(&drm) && events == 1);
    events = 0;
    assert(!dpu_runtime_resume(&p.dev) && events == 23);
}
''', flags=('-Wno-unused-parameter', '-Wno-unused-but-set-variable'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    bluetooth(args.source)
    hci_suspend(args.source)
    ibs_power_cycle(args.source)
    display(args.source)
    write_report(['BT IRQ ownership, TTY wake, console, closed port and PM failures',
                  'WCN6855 HCI suspend with an idle, completing or stalled request',
                  'WCN6855 IBS power-off with pending wake, timers, late UART data and reopen',
                  'DPU initial vote, retained rate, rebind and clock/OPP errors'], args.report)
