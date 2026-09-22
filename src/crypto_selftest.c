/*
 * Cornix PSA crypto self-test (CONFIG_CORNIX_CRYPTO_SELFTEST).
 *
 * Purpose: answer, on the device and from the log alone, the question "is
 * P-256 / ECDH / AES-CMAC computed correctly by *this* build?".
 *
 * Why it matters: an LE Secure Connections pairing that ends with
 * `Security failed ... err 1` (BT_SECURITY_ERR_AUTH_FAIL) failed because the
 * peer rejected our DHKey check or our confirm value -
 * zephyr/subsys/bluetooth/host/smp.c security_err_get() maps both
 * BT_SMP_ERR_DHKEY_CHECK_FAILED and BT_SMP_ERR_CONFIRM_FAILED to AUTH_FAIL,
 * and smp.c's own DHKey-check mismatch path returns without logging anything.
 * Every one of those values comes out of the ECDH shared secret (f5/f6) or
 * AES-CMAC (f4/f5/f6), so a wrong shared secret - or a wrong CMAC, or a
 * constant RNG - reproduces the symptom exactly.
 *
 * The checks deliberately use the *same* PSA Crypto entry points the
 * Bluetooth host uses, so a driver/toolchain difference shows up here the way
 * it would show up during pairing:
 *
 *   host/ecc.c  generate_pub_key():  psa_generate_key, psa_export_public_key,
 *                                    psa_export_key
 *   host/ecc.c  generate_dh_key():   psa_import_key (ECC_KEY_PAIR),
 *                                    psa_raw_key_agreement(PSA_ALG_ECDH, ...)
 *   host/ecc.c  bt_pub_key_is_valid(): psa_import_key (ECC_PUBLIC_KEY) of a
 *                                    0x04||X||Y blob
 *   crypto/bt_crypto_psa.c bt_crypto_aes_cmac(): psa_import_key (AES) +
 *                                    psa_mac_compute(PSA_ALG_CMAC, ...)
 *   host/crypto_psa.c bt_rand():     psa_generate_random
 *
 * Test vectors (both re-verified independently before being embedded):
 *   RFC 5903 section 8.1 - 256-bit Random ECP group (= NIST P-256 / secp256r1):
 *     initiator private i and public gix/giy, responder private r and public
 *     grx/gry, shared value girx/giry.  SMP's DHKey is exactly girx, the
 *     x-coordinate, which is also what psa_raw_key_agreement() returns for
 *     PSA_ALG_ECDH.
 *   RFC 4493 section 4 - AES-CMAC examples 1..4 (empty, 16, 40 and 64 byte
 *     messages), which cover both the "last block complete" (K1) and
 *     "last block padded" (K2) paths CMAC has.
 *
 * Everything in this file is behind CONFIG_CORNIX_CRYPTO_SELFTEST; with the
 * symbol off the file is not even compiled (root CMakeLists.txt).
 */

#include <stdarg.h>
#include <string.h>

#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include <psa/crypto.h>

LOG_MODULE_REGISTER(cornix_crypto, LOG_LEVEL_INF);

/* ------------------------------------------------------------------------ */
/* Test vectors                                                             */
/* ------------------------------------------------------------------------ */

#define P256_SCALAR_LEN 32
#define P256_COORD_LEN  32
/* Uncompressed SEC1 point: 0x04 || X || Y.  This is the layout PSA expects
 * for a secp256r1 public key, and the reason ecc.c prepends the 0x04 byte
 * itself before calling psa_raw_key_agreement().
 */
#define P256_POINT_LEN (1 + 2 * P256_COORD_LEN)

/* RFC 5903 8.1: initiator's private key "i". */
static const uint8_t rfc5903_i[P256_SCALAR_LEN] = {
	0xc8, 0x8f, 0x01, 0xf5, 0x10, 0xd9, 0xac, 0x3f,
	0x70, 0xa2, 0x92, 0xda, 0xa2, 0x31, 0x6d, 0xe5,
	0x44, 0xe9, 0xaa, 0xb8, 0xaf, 0xe8, 0x40, 0x49,
	0xc6, 0x2a, 0x9c, 0x57, 0x86, 0x2d, 0x14, 0x33,
};

/* RFC 5903 8.1: initiator's public key gi^x / gi^y, as 0x04 || X || Y. */
static const uint8_t rfc5903_gi[P256_POINT_LEN] = {
	0x04,
	/* gix */
	0xda, 0xd0, 0xb6, 0x53, 0x94, 0x22, 0x1c, 0xf9,
	0xb0, 0x51, 0xe1, 0xfe, 0xca, 0x57, 0x87, 0xd0,
	0x98, 0xdf, 0xe6, 0x37, 0xfc, 0x90, 0xb9, 0xef,
	0x94, 0x5d, 0x0c, 0x37, 0x72, 0x58, 0x11, 0x80,
	/* giy */
	0x52, 0x71, 0xa0, 0x46, 0x1c, 0xdb, 0x82, 0x52,
	0xd6, 0x1f, 0x1c, 0x45, 0x6f, 0xa3, 0xe5, 0x9a,
	0xb1, 0xf4, 0x5b, 0x33, 0xac, 0xcf, 0x5f, 0x58,
	0x38, 0x9e, 0x05, 0x77, 0xb8, 0x99, 0x0b, 0xb3,
};

/* RFC 5903 8.1: responder's private key "r". */
static const uint8_t rfc5903_r[P256_SCALAR_LEN] = {
	0xc6, 0xef, 0x9c, 0x5d, 0x78, 0xae, 0x01, 0x2a,
	0x01, 0x11, 0x64, 0xac, 0xb3, 0x97, 0xce, 0x20,
	0x88, 0x68, 0x5d, 0x8f, 0x06, 0xbf, 0x9b, 0xe0,
	0xb2, 0x83, 0xab, 0x46, 0x47, 0x6b, 0xee, 0x53,
};

/* RFC 5903 8.1: responder's public key gr^x / gr^y, as 0x04 || X || Y. */
static const uint8_t rfc5903_gr[P256_POINT_LEN] = {
	0x04,
	/* grx */
	0xd1, 0x2d, 0xfb, 0x52, 0x89, 0xc8, 0xd4, 0xf8,
	0x12, 0x08, 0xb7, 0x02, 0x70, 0x39, 0x8c, 0x34,
	0x22, 0x96, 0x97, 0x0a, 0x0b, 0xcc, 0xb7, 0x4c,
	0x73, 0x6f, 0xc7, 0x55, 0x44, 0x94, 0xbf, 0x63,
	/* gry */
	0x56, 0xfb, 0xf3, 0xca, 0x36, 0x6c, 0xc2, 0x3e,
	0x81, 0x57, 0x85, 0x4c, 0x13, 0xc5, 0x8d, 0x6a,
	0xac, 0x23, 0xf0, 0x46, 0xad, 0xa3, 0x0f, 0x83,
	0x53, 0xe7, 0x4f, 0x33, 0x03, 0x98, 0x72, 0xab,
};

/* RFC 5903 8.1: the Diffie-Hellman common value's x-coordinate gir^x.  This is
 * what an ECDH raw key agreement must return, and what SMP calls "DHKey".
 */
static const uint8_t rfc5903_girx[P256_COORD_LEN] = {
	0xd6, 0x84, 0x0f, 0x6b, 0x42, 0xf6, 0xed, 0xaf,
	0xd1, 0x31, 0x16, 0xe0, 0xe1, 0x25, 0x65, 0x20,
	0x2f, 0xef, 0x8e, 0x9e, 0xce, 0x7d, 0xce, 0x03,
	0x81, 0x24, 0x64, 0xd0, 0x4b, 0x94, 0x42, 0xde,
};

/* RFC 4493 section 4: the AES-128 key used by all four examples. */
static const uint8_t rfc4493_key[16] = {
	0x2b, 0x7e, 0x15, 0x16, 0x28, 0xae, 0xd2, 0xa6,
	0xab, 0xf7, 0x15, 0x88, 0x09, 0xcf, 0x4f, 0x3c,
};

/* RFC 4493 section 4: the 64-byte message; examples 1..4 use its first
 * 0 / 16 / 40 / 64 bytes.
 */
static const uint8_t rfc4493_msg[64] = {
	0x6b, 0xc1, 0xbe, 0xe2, 0x2e, 0x40, 0x9f, 0x96,
	0xe9, 0x3d, 0x7e, 0x11, 0x73, 0x93, 0x17, 0x2a,
	0xae, 0x2d, 0x8a, 0x57, 0x1e, 0x03, 0xac, 0x9c,
	0x9e, 0xb7, 0x6f, 0xac, 0x45, 0xaf, 0x8e, 0x51,
	0x30, 0xc8, 0x1c, 0x46, 0xa3, 0x5c, 0xe4, 0x11,
	0xe5, 0xfb, 0xc1, 0x19, 0x1a, 0x0a, 0x52, 0xef,
	0xf6, 0x9f, 0x24, 0x45, 0xdf, 0x4f, 0x9b, 0x17,
	0xad, 0x2b, 0x41, 0x7b, 0xe6, 0x6c, 0x37, 0x10,
};

struct cmac_case {
	const char *name;
	size_t len;
	uint8_t mac[16];
};

static const struct cmac_case rfc4493_cases[] = {
	{ "len0", 0, { 0xbb, 0x1d, 0x69, 0x29, 0xe9, 0x59, 0x37, 0x28,
		       0x7f, 0xa3, 0x7d, 0x12, 0x9b, 0x75, 0x67, 0x46 } },
	{ "len16", 16, { 0x07, 0x0a, 0x16, 0xb4, 0x6b, 0x4d, 0x41, 0x44,
			 0xf7, 0x9b, 0xdd, 0x9d, 0xd0, 0x4a, 0x28, 0x7c } },
	{ "len40", 40, { 0xdf, 0xa6, 0x67, 0x47, 0xde, 0x9a, 0xe6, 0x30,
			 0x30, 0xca, 0x32, 0x61, 0x14, 0x97, 0xc8, 0x27 } },
	{ "len64", 64, { 0x51, 0xf0, 0xbe, 0xbf, 0x7e, 0x3b, 0x9d, 0x92,
			 0xfc, 0x49, 0x74, 0x17, 0x79, 0x36, 0x3c, 0xfe } },
};

/* ------------------------------------------------------------------------ */
/* Reporting                                                                */
/* ------------------------------------------------------------------------ */

static unsigned int checks_run;
static unsigned int checks_failed;

static void report(const char *name, bool ok, const char *fmt, ...)
{
	static char details[96];
	va_list ap;

	checks_run++;
	if (!ok) {
		checks_failed++;
	}

	va_start(ap, fmt);
	vsnprintk(details, sizeof(details), fmt, ap);
	va_end(ap);

	if (ok) {
		LOG_INF("crypto selftest: %s PASS (%s)", name, details);
	} else {
		LOG_ERR("crypto selftest: %s FAIL (%s)", name, details);
	}
}

/* Short, log-friendly rendering of a buffer: first 8 bytes as hex. */
static const char *head8(const uint8_t *buf, size_t len, char *out, size_t out_len)
{
	size_t n = MIN(len, (size_t)8);

	if (bin2hex(buf, n, out, out_len) == 0) {
		out[0] = '\0';
	}
	return out;
}

/* ------------------------------------------------------------------------ */
/* PSA helpers (mirroring zephyr/subsys/bluetooth/host/ecc.c)               */
/* ------------------------------------------------------------------------ */

static void set_keypair_attributes(psa_key_attributes_t *attr)
{
	/* ecc.c set_key_attributes(), verbatim. */
	psa_set_key_type(attr, PSA_KEY_TYPE_ECC_KEY_PAIR(PSA_ECC_FAMILY_SECP_R1));
	psa_set_key_bits(attr, 256);
	psa_set_key_usage_flags(attr, PSA_KEY_USAGE_EXPORT | PSA_KEY_USAGE_DERIVE);
	psa_set_key_algorithm(attr, PSA_ALG_ECDH);
}

/* Import a raw 32-byte big-endian secp256r1 private scalar. */
static psa_status_t import_private(const uint8_t priv[P256_SCALAR_LEN], psa_key_id_t *key_id)
{
	psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_status_t status;

	set_keypair_attributes(&attr);
	status = psa_import_key(&attr, priv, P256_SCALAR_LEN, key_id);
	psa_reset_key_attributes(&attr);

	return status;
}

/* ------------------------------------------------------------------------ */
/* Checks                                                                   */
/* ------------------------------------------------------------------------ */

/* (a) public key derivation: import the private scalar, export the public key
 * and compare with the vector.  This is generate_pub_key()'s export path with
 * a known answer.
 */
static void check_pubkey(const char *name, const uint8_t priv[P256_SCALAR_LEN],
			 const uint8_t expect[P256_POINT_LEN])
{
	uint8_t pub[P256_POINT_LEN];
	char hex[24];
	psa_key_id_t key_id;
	psa_status_t status;
	size_t len = 0;

	status = import_private(priv, &key_id);
	if (status != PSA_SUCCESS) {
		report(name, false, "psa_import_key=%d", (int)status);
		return;
	}

	status = psa_export_public_key(key_id, pub, sizeof(pub), &len);
	psa_destroy_key(key_id);

	if (status != PSA_SUCCESS) {
		report(name, false, "psa_export_public_key=%d", (int)status);
		return;
	}
	if (len != P256_POINT_LEN) {
		report(name, false, "len=%u want %u", (unsigned int)len,
		       (unsigned int)P256_POINT_LEN);
		return;
	}
	if (memcmp(pub, expect, P256_POINT_LEN) != 0) {
		report(name, false, "got 04|%s", head8(&pub[1], P256_COORD_LEN, hex, sizeof(hex)));
		return;
	}

	report(name, true, "X=%s..", head8(&pub[1], P256_COORD_LEN, hex, sizeof(hex)));
}

/* Import a public key the way bt_pub_key_is_valid() does.  A failure here is
 * not the AUTH_FAIL path (smp.c answers BT_SMP_ERR_INVALID_PARAMS for it) but
 * it is one psa_import_key() flavour more than the key-pair one, and it is
 * free to check.
 */
static void check_pubkey_import(void)
{
	psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_key_id_t key_id;
	psa_status_t status;

	psa_set_key_type(&attr, PSA_KEY_TYPE_ECC_PUBLIC_KEY(PSA_ECC_FAMILY_SECP_R1));
	psa_set_key_bits(&attr, 256);
	psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_DERIVE);
	psa_set_key_algorithm(&attr, PSA_ALG_ECDH);

	status = psa_import_key(&attr, rfc5903_gi, sizeof(rfc5903_gi), &key_id);
	psa_reset_key_attributes(&attr);

	if (status != PSA_SUCCESS) {
		report("pubkey-import", false, "psa_import_key=%d", (int)status);
		return;
	}

	psa_destroy_key(key_id);
	report("pubkey-import", true, "gi accepted as ECC_PUBLIC_KEY");
}

/* (b) ECDH from each side: raw key agreement must return gir^x both ways.
 * This is generate_dh_key() with a known answer - the value SMP feeds into
 * f5/f6 and therefore the one a wrong result turns into a DHKey check
 * failure.
 */
static void check_ecdh(const char *name, const uint8_t priv[P256_SCALAR_LEN],
		       const uint8_t peer[P256_POINT_LEN])
{
	uint8_t dhkey[P256_COORD_LEN];
	char hex[24];
	psa_key_id_t key_id;
	psa_status_t status;
	size_t len = 0;

	status = import_private(priv, &key_id);
	if (status != PSA_SUCCESS) {
		report(name, false, "psa_import_key=%d", (int)status);
		return;
	}

	status = psa_raw_key_agreement(PSA_ALG_ECDH, key_id, peer, P256_POINT_LEN,
				       dhkey, sizeof(dhkey), &len);
	psa_destroy_key(key_id);

	if (status != PSA_SUCCESS) {
		report(name, false, "psa_raw_key_agreement=%d", (int)status);
		return;
	}
	if (len != P256_COORD_LEN) {
		report(name, false, "len=%u want %u", (unsigned int)len,
		       (unsigned int)P256_COORD_LEN);
		return;
	}
	if (memcmp(dhkey, rfc5903_girx, P256_COORD_LEN) != 0) {
		report(name, false, "got %s.. want %s..",
		       head8(dhkey, sizeof(dhkey), hex, sizeof(hex)),
		       "d6840f6b42f6edaf");
		return;
	}

	report(name, true, "DHKey=%s..", head8(dhkey, sizeof(dhkey), hex, sizeof(hex)));
}

/* (c) random key pair round trip: generate a key the way the host does for
 * every pairing, export its public key, and check that the agreement is
 * symmetric against the fixed RFC 5903 initiator - ECDH(gen_priv, gi) must
 * equal ECDH(i, gen_pub).  This catches a broken psa_generate_key() /
 * psa_export_public_key() pair that the fixed vectors above cannot see,
 * because those never exercise key generation.
 */
static void check_random_roundtrip(void)
{
	psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
	uint8_t gen_pub[P256_POINT_LEN];
	uint8_t secret_a[P256_COORD_LEN];
	uint8_t secret_b[P256_COORD_LEN];
	char hex_a[24];
	char hex_b[24];
	psa_key_id_t gen_id, fixed_id;
	psa_status_t status;
	size_t len = 0;

	set_keypair_attributes(&attr);
	status = psa_generate_key(&attr, &gen_id);
	psa_reset_key_attributes(&attr);
	if (status != PSA_SUCCESS) {
		report("ecdh-roundtrip", false, "psa_generate_key=%d", (int)status);
		return;
	}

	status = psa_export_public_key(gen_id, gen_pub, sizeof(gen_pub), &len);
	if (status != PSA_SUCCESS || len != P256_POINT_LEN) {
		psa_destroy_key(gen_id);
		report("ecdh-roundtrip", false, "psa_export_public_key=%d len=%u",
		       (int)status, (unsigned int)len);
		return;
	}

	/* our side: ECDH(generated private, fixed gi) */
	status = psa_raw_key_agreement(PSA_ALG_ECDH, gen_id, rfc5903_gi, sizeof(rfc5903_gi),
				       secret_a, sizeof(secret_a), &len);
	psa_destroy_key(gen_id);
	if (status != PSA_SUCCESS) {
		report("ecdh-roundtrip", false, "agree(gen,gi)=%d", (int)status);
		return;
	}

	/* peer side: ECDH(fixed i, generated public) */
	status = import_private(rfc5903_i, &fixed_id);
	if (status != PSA_SUCCESS) {
		report("ecdh-roundtrip", false, "psa_import_key=%d", (int)status);
		return;
	}
	status = psa_raw_key_agreement(PSA_ALG_ECDH, fixed_id, gen_pub, sizeof(gen_pub),
				       secret_b, sizeof(secret_b), &len);
	psa_destroy_key(fixed_id);
	if (status != PSA_SUCCESS) {
		report("ecdh-roundtrip", false, "agree(i,gen)=%d", (int)status);
		return;
	}

	if (memcmp(secret_a, secret_b, P256_COORD_LEN) != 0) {
		report("ecdh-roundtrip", false, "asymmetric: %s.. vs %s..",
		       head8(secret_a, sizeof(secret_a), hex_a, sizeof(hex_a)),
		       head8(secret_b, sizeof(secret_b), hex_b, sizeof(hex_b)));
		return;
	}

	report("ecdh-roundtrip", true, "shared=%s..",
	       head8(secret_a, sizeof(secret_a), hex_a, sizeof(hex_a)));
}

/* AES-CMAC, the primitive behind SMP's f4 (confirm), f5 (LTK/MacKey) and f6
 * (DHKey check).  bt_crypto_aes_cmac() byte-swaps its inputs; this checks the
 * PSA layer underneath it, which is where a driver problem would live.
 */
static void check_cmac(void)
{
	psa_key_attributes_t attr = PSA_KEY_ATTRIBUTES_INIT;
	psa_key_id_t key_id;
	psa_status_t status;

	psa_set_key_type(&attr, PSA_KEY_TYPE_AES);
	psa_set_key_bits(&attr, 128);
	psa_set_key_usage_flags(&attr, PSA_KEY_USAGE_SIGN_MESSAGE);
	psa_set_key_algorithm(&attr, PSA_ALG_CMAC);

	status = psa_import_key(&attr, rfc4493_key, sizeof(rfc4493_key), &key_id);
	psa_reset_key_attributes(&attr);
	if (status != PSA_SUCCESS) {
		report("cmac", false, "psa_import_key=%d", (int)status);
		return;
	}

	for (size_t i = 0; i < ARRAY_SIZE(rfc4493_cases); i++) {
		const struct cmac_case *tc = &rfc4493_cases[i];
		char name[24];
		char hex[24];
		uint8_t mac[16];
		size_t mac_len = 0;

		snprintk(name, sizeof(name), "cmac-%s", tc->name);

		status = psa_mac_compute(key_id, PSA_ALG_CMAC, rfc4493_msg, tc->len,
					 mac, sizeof(mac), &mac_len);
		if (status != PSA_SUCCESS) {
			report(name, false, "psa_mac_compute=%d", (int)status);
			continue;
		}
		if (mac_len != sizeof(mac) || memcmp(mac, tc->mac, sizeof(mac)) != 0) {
			report(name, false, "got %s.. len=%u",
			       head8(mac, sizeof(mac), hex, sizeof(hex)),
			       (unsigned int)mac_len);
			continue;
		}
		report(name, true, "%s..", head8(mac, sizeof(mac), hex, sizeof(hex)));
	}

	psa_destroy_key(key_id);
}

/* psa_generate_random() is bt_rand() (host/crypto_psa.c) when
 * CONFIG_BT_HOST_CRYPTO_PRNG is set: SMP's local random Na/Nb come from it.
 * A stuck or constant RNG makes every confirm check fail.  This is a smoke
 * test, not a statistical one: three draws, none all-zero, all different.
 */
static void check_random(void)
{
	uint8_t draw[3][16];
	char hex[24];
	psa_status_t status;
	static const uint8_t zeros[16] = { 0 };

	for (size_t i = 0; i < ARRAY_SIZE(draw); i++) {
		status = psa_generate_random(draw[i], sizeof(draw[i]));
		if (status != PSA_SUCCESS) {
			report("random", false, "psa_generate_random=%d", (int)status);
			return;
		}
		if (memcmp(draw[i], zeros, sizeof(zeros)) == 0) {
			report("random", false, "draw %u all zero", (unsigned int)i);
			return;
		}
	}

	if (memcmp(draw[0], draw[1], sizeof(draw[0])) == 0 ||
	    memcmp(draw[1], draw[2], sizeof(draw[0])) == 0 ||
	    memcmp(draw[0], draw[2], sizeof(draw[0])) == 0) {
		report("random", false, "constant output %s..",
		       head8(draw[0], sizeof(draw[0]), hex, sizeof(hex)));
		return;
	}

	report("random", true, "3 distinct draws, first %s..",
	       head8(draw[0], sizeof(draw[0]), hex, sizeof(hex)));
}

/* ------------------------------------------------------------------------ */
/* Runner                                                                   */
/* ------------------------------------------------------------------------ */

static void run_selftest(void)
{
	psa_status_t status;

	/* Zephyr's mbedTLS module already calls this from a POST_KERNEL SYS_INIT
	 * (modules/mbedtls/zephyr_init.c, under CONFIG_MBEDTLS_INIT), and so does
	 * bt_crypto_init() (host/crypto_psa.c) at bt_enable() time.  It is
	 * idempotent, so calling it again both makes this file work in a build
	 * without either and reports the status of the shared initialisation.
	 */
	status = psa_crypto_init();
	report("psa-init", status == PSA_SUCCESS, "psa_crypto_init=%d", (int)status);
	if (status != PSA_SUCCESS) {
		LOG_ERR("crypto selftest: FAILED %u of %u (PSA unusable)", checks_failed,
			checks_run);
		return;
	}

	LOG_INF("crypto selftest: driver=%s",
		IS_ENABLED(CONFIG_MBEDTLS_PSA_P256M_DRIVER_ENABLED) ? "p256-m" : "mbedtls-ecp");

	check_pubkey("pubkey-i", rfc5903_i, rfc5903_gi);
	check_pubkey("pubkey-r", rfc5903_r, rfc5903_gr);
	check_pubkey_import();
	check_ecdh("ecdh-i", rfc5903_i, rfc5903_gr);
	check_ecdh("ecdh-r", rfc5903_r, rfc5903_gi);
	check_random_roundtrip();
	check_cmac();
	check_random();

	if (checks_failed == 0) {
		LOG_INF("crypto selftest: ALL PASS (%u checks)", checks_run);
	} else {
		LOG_ERR("crypto selftest: FAILED %u of %u", checks_failed, checks_run);
	}
}

static K_THREAD_STACK_DEFINE(selftest_stack, CONFIG_CORNIX_CRYPTO_SELFTEST_STACK_SIZE);
static struct k_thread selftest_thread;

static void selftest_entry(void *a, void *b, void *c)
{
	ARG_UNUSED(a);
	ARG_UNUSED(b);
	ARG_UNUSED(c);

	if (CONFIG_CORNIX_CRYPTO_SELFTEST_DELAY_MS > 0) {
		k_sleep(K_MSEC(CONFIG_CORNIX_CRYPTO_SELFTEST_DELAY_MS));
	}

	run_selftest();
}

static int cornix_crypto_selftest_init(void)
{
	/* Preemptible, lowest-but-one priority: the test must never delay the
	 * keyboard's own work, and a P-256 scalar multiplication takes a while.
	 */
	k_thread_create(&selftest_thread, selftest_stack,
			K_THREAD_STACK_SIZEOF(selftest_stack), selftest_entry,
			NULL, NULL, NULL, K_LOWEST_APPLICATION_THREAD_PRIO, 0, K_NO_WAIT);
	k_thread_name_set(&selftest_thread, "cornix_selftest");

	return 0;
}

SYS_INIT(cornix_crypto_selftest_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
