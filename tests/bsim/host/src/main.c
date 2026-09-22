/*
 * The simulated host computer (the "Mac") of the three-device Cornix
 * BabbleSim simulation.
 *
 * SPDX-License-Identifier: MIT
 *
 * On real hardware the left half is not only the split BLE *central* of the
 * right half, it is at the same time a BLE *peripheral* of the computer it
 * types into. Its controller therefore has to schedule two independent
 * connections. The two-device simulation in tests/bsim/split-latency has only
 * the split link, which is why it never reproduced the several-hundred
 * millisecond lag seen on hardware with
 * CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=30.
 *
 * This application adds the missing link. It is a plain Zephyr BLE central
 * (no ZMK), derived from zephyr/samples/bluetooth/central_hr but pointed at
 * the HID service:
 *
 *   1. active scan, connect to the advertiser that carries BOTH the HID
 *      service UUID 0x1812 *and* a name containing the configured filter
 *      (default "Cornix"). The split peripheral advertises the 128-bit split
 *      service UUID and no name, so it is never picked up.
 *   2. request just-works pairing (BT_SECURITY_L2) -- ZMK's HID
 *      characteristics are BT_GATT_PERM_*_ENCRYPT, so an unencrypted link
 *      cannot even write the CCC.
 *   3. discover 0x1812, then every 0x2A4D report characteristic with the
 *      notify property, then each one's 0x2902 CCC, and subscribe.
 *   4. log every notification with the simulated timestamp, so that
 *      scripts/bsim/measure.py can compute "peripheral key -> host HID
 *      report" end to end.
 *
 * The connection parameters are the interesting knob: they are what makes
 * this link compete with the split link for radio time. They default to a
 * macOS-like 15 ms / latency 0 / 4 s and can be overridden per run on the
 * BabbleSim command line, e.g.
 *
 *   ./cornix_host.exe -s=sim -d=2 -host_interval=24 -host_latency=4
 */

#include <errno.h>
#include <stddef.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/types.h>

#include "cmdline.h" /* native_add_command_line_opts() -> bs_add_extra_dynargs() */
#include <posix_native_task.h> /* NATIVE_TASK() -- runs before the kernel boots */

LOG_MODULE_REGISTER(cornix_host, LOG_LEVEL_DBG);

/* Room for ZMK's keyboard / consumer / mouse input reports. */
#define MAX_SUBSCRIPTIONS 4
#define NAME_FILTER_LEN 32

/* --- run-time configuration ------------------------------------------- */

/* BabbleSim's bs_args_set_defaults() resets every registered option to a
 * "not given" marker (UINT32_MAX for 'u', NULL for 's') before parsing, so the
 * Kconfig defaults are applied in apply_arg_defaults() afterwards rather than
 * as C initialisers.
 */
#define ARG_UNSET UINT32_MAX

static uint32_t arg_interval = ARG_UNSET;
static uint32_t arg_latency = ARG_UNSET;
static uint32_t arg_timeout = ARG_UNSET;
static uint32_t arg_accept_updates = ARG_UNSET;
static const char *arg_name_filter;

static void apply_arg_defaults(void) {
	if (arg_interval == ARG_UNSET) {
		arg_interval = CONFIG_CORNIX_HOST_INTERVAL;
	}
	if (arg_latency == ARG_UNSET) {
		arg_latency = CONFIG_CORNIX_HOST_LATENCY;
	}
	if (arg_timeout == ARG_UNSET) {
		arg_timeout = CONFIG_CORNIX_HOST_TIMEOUT;
	}
	if (arg_accept_updates == ARG_UNSET) {
		arg_accept_updates = IS_ENABLED(CONFIG_CORNIX_HOST_ACCEPT_PARAM_UPDATE);
	}
	if (arg_name_filter == NULL) {
		arg_name_filter = CONFIG_CORNIX_HOST_NAME_FILTER;
	}
	/* "*" means "any HID advertiser", for a run without a name filter. */
	if (strcmp(arg_name_filter, "*") == 0) {
		arg_name_filter = "";
	}
}

static void register_cmdline_opts(void) {
	static struct args_struct_t opts[] = {
		{false, false, false, "host_interval", "units", 'u', (void *)&arg_interval, NULL,
		 "Connection interval the host requests, in 1.25 ms units (default "
		 STRINGIFY(CONFIG_CORNIX_HOST_INTERVAL) ")"},
		{false, false, false, "host_latency", "events", 'u', (void *)&arg_latency, NULL,
		 "Peripheral latency the host requests (default "
		 STRINGIFY(CONFIG_CORNIX_HOST_LATENCY) ")"},
		{false, false, false, "host_timeout", "units", 'u', (void *)&arg_timeout, NULL,
		 "Supervision timeout the host requests, in 10 ms units (default "
		 STRINGIFY(CONFIG_CORNIX_HOST_TIMEOUT) ")"},
		{false, false, false, "host_accept_updates", "0/1", 'u', (void *)&arg_accept_updates,
		 NULL, "Accept the keyboard's own connection parameter update requests"},
		{false, false, false, "host_name", "string", 's', (void *)&arg_name_filter, NULL,
		 "Only connect to an advertiser whose name contains this (\"*\" = any)"},
		ARG_TABLE_ENDMARKER};

	native_add_command_line_opts(opts);
}
/* The BabbleSim command line is parsed before the Zephyr kernel starts, so the
 * options have to be registered from a PRE_BOOT task, not from SYS_INIT.
 */
NATIVE_TASK(register_cmdline_opts, PRE_BOOT_1, 100);

/* --- state ------------------------------------------------------------ */

static struct bt_conn *default_conn;

static struct bt_uuid_16 discover_uuid = BT_UUID_INIT_16(0);
static struct bt_gatt_discover_params discover_params;
static struct bt_gatt_subscribe_params subscribe_params[MAX_SUBSCRIPTIONS];

static uint16_t report_value_handles[MAX_SUBSCRIPTIONS];
static uint8_t report_count;      /* notifiable 0x2A4D characteristics found */
static uint8_t report_cursor;     /* the one whose CCC we are looking for */
static uint8_t subscribed_count;  /* successfully subscribed */
static uint16_t hids_end_handle;

static uint32_t notification_count;

static void start_scan(void);
static void discover_next_ccc(void);

/* --- notifications ---------------------------------------------------- */

static uint8_t notify_func(struct bt_conn *conn, struct bt_gatt_subscribe_params *params,
			   const void *data, uint16_t length) {
	const uint8_t *bytes = data;
	char hex[3 * 20 + 1];
	size_t off = 0;

	if (!data) {
		LOG_INF("[HOST UNSUBSCRIBED] handle %u", params->value_handle);
		params->value_handle = 0U;
		return BT_GATT_ITER_STOP;
	}

	for (uint16_t i = 0; i < length && off + 3 < sizeof(hex); i++) {
		off += snprintk(&hex[off], sizeof(hex) - off, "%02x", bytes[i]);
	}
	hex[off] = '\0';

	notification_count++;
	/* measure.py keys on this exact prefix. */
	LOG_INF("[HOST HID REPORT] n=%u handle=%u length=%u data=%s", notification_count,
		params->value_handle, length, hex);

	return BT_GATT_ITER_CONTINUE;
}

/* --- GATT discovery --------------------------------------------------- */

static uint8_t ccc_discover_func(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				 struct bt_gatt_discover_params *params) {
	int err;

	if (!attr) {
		/* No CCC for this one (an output/feature report); try the next. */
		report_cursor++;
		discover_next_ccc();
		return BT_GATT_ITER_STOP;
	}

	subscribe_params[report_cursor].notify = notify_func;
	subscribe_params[report_cursor].value = BT_GATT_CCC_NOTIFY;
	subscribe_params[report_cursor].value_handle = report_value_handles[report_cursor];
	subscribe_params[report_cursor].ccc_handle = attr->handle;

	err = bt_gatt_subscribe(conn, &subscribe_params[report_cursor]);
	if (err && err != -EALREADY) {
		LOG_ERR("subscribe to handle %u failed (%d)", report_value_handles[report_cursor],
			err);
	} else {
		subscribed_count++;
		LOG_INF("[HOST SUBSCRIBED] report value handle %u ccc %u (%u of %u)",
			report_value_handles[report_cursor], attr->handle, subscribed_count,
			report_count);
	}

	report_cursor++;
	discover_next_ccc();
	return BT_GATT_ITER_STOP;
}

static void discover_next_ccc(void) {
	int err;

	if (report_cursor >= report_count) {
		LOG_INF("[HOST READY] subscribed to %u of %u HID report characteristics",
			subscribed_count, report_count);
		return;
	}

	memcpy(&discover_uuid, BT_UUID_GATT_CCC, sizeof(discover_uuid));
	discover_params.uuid = &discover_uuid.uuid;
	discover_params.start_handle = report_value_handles[report_cursor] + 1;
	discover_params.end_handle = hids_end_handle;
	discover_params.type = BT_GATT_DISCOVER_DESCRIPTOR;
	discover_params.func = ccc_discover_func;

	err = bt_gatt_discover(default_conn, &discover_params);
	if (err) {
		LOG_ERR("CCC discovery failed (%d)", err);
	}
}

static uint8_t chrc_discover_func(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				  struct bt_gatt_discover_params *params) {
	const struct bt_gatt_chrc *chrc;

	if (!attr) {
		LOG_INF("found %u notifiable HID report characteristics", report_count);
		report_cursor = 0;
		discover_next_ccc();
		return BT_GATT_ITER_STOP;
	}

	chrc = attr->user_data;
	if ((chrc->properties & BT_GATT_CHRC_NOTIFY) && report_count < MAX_SUBSCRIPTIONS) {
		report_value_handles[report_count] = bt_gatt_attr_value_handle(attr);
		LOG_INF("HID report characteristic, value handle %u, properties 0x%02x",
			report_value_handles[report_count], chrc->properties);
		report_count++;
	}

	return BT_GATT_ITER_CONTINUE;
}

static uint8_t svc_discover_func(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				 struct bt_gatt_discover_params *params) {
	const struct bt_gatt_service_val *svc;
	int err;

	if (!attr) {
		LOG_ERR("no HID service (0x1812) on the peer");
		return BT_GATT_ITER_STOP;
	}

	svc = attr->user_data;
	hids_end_handle = svc->end_handle;
	LOG_INF("HID service at handles %u..%u", attr->handle, hids_end_handle);

	memcpy(&discover_uuid, BT_UUID_HIDS_REPORT, sizeof(discover_uuid));
	discover_params.uuid = &discover_uuid.uuid;
	discover_params.start_handle = attr->handle + 1;
	discover_params.end_handle = hids_end_handle;
	discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
	discover_params.func = chrc_discover_func;

	err = bt_gatt_discover(conn, &discover_params);
	if (err) {
		LOG_ERR("characteristic discovery failed (%d)", err);
	}

	return BT_GATT_ITER_STOP;
}

static void start_discovery(struct bt_conn *conn) {
	int err;

	report_count = 0;
	report_cursor = 0;
	subscribed_count = 0;

	memcpy(&discover_uuid, BT_UUID_HIDS, sizeof(discover_uuid));
	discover_params.uuid = &discover_uuid.uuid;
	discover_params.func = svc_discover_func;
	discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
	discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;
	discover_params.type = BT_GATT_DISCOVER_PRIMARY;

	err = bt_gatt_discover(conn, &discover_params);
	if (err) {
		LOG_ERR("service discovery failed (%d)", err);
	}
}

/* --- scanning --------------------------------------------------------- */

struct ad_scan_result {
	const bt_addr_le_t *addr;
	bool has_hid;
	bool name_matches;
	char name[NAME_FILTER_LEN];
};

static bool ad_parse(struct bt_data *data, void *user_data) {
	struct ad_scan_result *res = user_data;

	switch (data->type) {
	case BT_DATA_UUID16_SOME:
	case BT_DATA_UUID16_ALL:
		for (int i = 0; i + 1 < data->data_len; i += sizeof(uint16_t)) {
			uint16_t u16;

			memcpy(&u16, &data->data[i], sizeof(u16));
			if (sys_le16_to_cpu(u16) == BT_UUID_HIDS_VAL) {
				res->has_hid = true;
			}
		}
		break;
	case BT_DATA_NAME_SHORTENED:
	case BT_DATA_NAME_COMPLETE: {
		size_t len = MIN(data->data_len, sizeof(res->name) - 1);

		memcpy(res->name, data->data, len);
		res->name[len] = '\0';
		if (arg_name_filter[0] == '\0' || strstr(res->name, arg_name_filter) != NULL) {
			res->name_matches = true;
		}
		break;
	}
	default:
		break;
	}

	return true;
}

static void device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			 struct net_buf_simple *ad) {
	struct ad_scan_result res = {.addr = addr};
	struct bt_le_conn_param param;
	char dev[BT_ADDR_LE_STR_LEN];
	int err;

	if (default_conn) {
		return;
	}
	if (type != BT_GAP_ADV_TYPE_ADV_IND && type != BT_GAP_ADV_TYPE_ADV_DIRECT_IND &&
	    type != BT_GAP_ADV_TYPE_SCAN_RSP && type != BT_GAP_ADV_TYPE_EXT_ADV) {
		return;
	}

	bt_data_parse(ad, ad_parse, &res);
	if (!res.has_hid || !res.name_matches) {
		return;
	}

	bt_addr_le_to_str(addr, dev, sizeof(dev));
	LOG_INF("[HOST FOUND] keyboard \"%s\" at %s rssi %d", res.name, dev, rssi);

	err = bt_le_scan_stop();
	if (err) {
		LOG_ERR("scan stop failed (%d)", err);
		return;
	}

	param.interval_min = (uint16_t)arg_interval;
	param.interval_max = (uint16_t)arg_interval;
	param.latency = (uint16_t)arg_latency;
	param.timeout = (uint16_t)arg_timeout;

	LOG_INF("[HOST CONNECTING] requesting interval %u (%u.%02u ms) latency %u timeout %u",
		arg_interval, (arg_interval * 125) / 100, (arg_interval * 125) % 100, arg_latency,
		arg_timeout);

	err = bt_conn_le_create(addr, BT_CONN_LE_CREATE_CONN, &param, &default_conn);
	if (err) {
		LOG_ERR("create connection failed (%d)", err);
		start_scan();
	}
}

static void start_scan(void) {
	struct bt_le_scan_param scan_param = {
		.type = BT_LE_SCAN_TYPE_ACTIVE,
		.options = BT_LE_SCAN_OPT_NONE,
		.interval = BT_GAP_SCAN_FAST_INTERVAL,
		.window = BT_GAP_SCAN_FAST_WINDOW,
	};
	int err = bt_le_scan_start(&scan_param, device_found);

	if (err) {
		LOG_ERR("scan start failed (%d)", err);
		return;
	}
	LOG_INF("[HOST SCANNING] for a HID advertiser named \"%s\"", arg_name_filter);
}

/* --- connection callbacks --------------------------------------------- */

static void connected(struct bt_conn *conn, uint8_t conn_err) {
	char addr[BT_ADDR_LE_STR_LEN];
	int err;

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	if (conn_err) {
		LOG_ERR("[HOST CONNECT FAILED] %s (%u)", addr, conn_err);
		bt_conn_unref(default_conn);
		default_conn = NULL;
		start_scan();
		return;
	}

	LOG_INF("[HOST CONNECTED] %s", addr);

	/* The parameters the link actually came up with, before any update. */
	struct bt_conn_info info;

	if (bt_conn_get_info(conn, &info) == 0) {
		LOG_INF("[HOST PARAMS] interval %u latency %u timeout %u", info.le.interval,
			info.le.latency, info.le.timeout);
	}

	/* The HID characteristics need an encrypted link before the CCC can be
	 * written, so pair first and discover from security_changed().
	 */
	err = bt_conn_set_security(conn, BT_SECURITY_L2);
	if (err) {
		LOG_ERR("set security failed (%d), discovering anyway", err);
		start_discovery(conn);
	}
}

static void disconnected(struct bt_conn *conn, uint8_t reason) {
	char addr[BT_ADDR_LE_STR_LEN];

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));
	LOG_WRN("[HOST DISCONNECTED] %s reason 0x%02x", addr, reason);

	if (default_conn != conn) {
		return;
	}
	bt_conn_unref(default_conn);
	default_conn = NULL;
	memset(subscribe_params, 0, sizeof(subscribe_params));
	start_scan();
}

static void security_changed(struct bt_conn *conn, bt_security_t level, enum bt_security_err err) {
	if (err) {
		LOG_ERR("[HOST SECURITY FAILED] level %d err %d", level, err);
		return;
	}
	LOG_INF("[HOST SECURED] level %d", level);
	if (level >= BT_SECURITY_L2 && report_count == 0) {
		start_discovery(conn);
	}
}

static bool le_param_req(struct bt_conn *conn, struct bt_le_conn_param *param) {
	LOG_INF("[HOST PARAM REQ] keyboard asks for interval %u-%u latency %u timeout %u -> %s",
		param->interval_min, param->interval_max, param->latency, param->timeout,
		arg_accept_updates ? "accept" : "reject");
	return arg_accept_updates != 0;
}

static void le_param_updated(struct bt_conn *conn, uint16_t interval, uint16_t latency,
			     uint16_t timeout) {
	LOG_INF("[HOST PARAMS] interval %u latency %u timeout %u", interval, latency, timeout);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
	.security_changed = security_changed,
	.le_param_req = le_param_req,
	.le_param_updated = le_param_updated,
};

/* --- entry point ------------------------------------------------------ */

int main(void) {
	int err;

	apply_arg_defaults();
	err = bt_enable(NULL);

	if (err) {
		LOG_ERR("bt_enable failed (%d)", err);
		return 0;
	}

	LOG_INF("[HOST START] interval %u latency %u timeout %u accept_updates %u filter \"%s\"",
		arg_interval, arg_latency, arg_timeout, arg_accept_updates, arg_name_filter);
	start_scan();
	return 0;
}
