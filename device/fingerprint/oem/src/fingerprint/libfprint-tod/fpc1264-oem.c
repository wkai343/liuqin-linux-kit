/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Explicit TOD adapter for the OEM FPC1264 match path. No software matching.
 * FpPrint version 2 contains an immutable per-finger reference into a
 * root-private account gallery; old opaque-print formats are rejected.
 * fprintd owns account authorization and publication. Trusted helpers own the
 * hardware transaction and durable encrypted-gallery update.
 */
#include <gio/gio.h>
#include <gmodule.h>
#include <glib/gstdio.h>
#include <fpi-device.h>
#include <fpi-print.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <pwd.h>
#include "../protocol/fpc_gallery.h"

typedef struct {
  FpDevice parent;
  GSubprocess *child;
  GPtrArray *gallery;
  gchar *request;
  gchar *result;
  gboolean identifying;
  gchar *directory;
  gchar *database;
  FpPrint *enrolled_print;
  gboolean cancelled;
  GDataInputStream *enrol_output;
  GError *enrol_error;
  gint enrol_stages;
  gint enrol_completed;
  guint enrol_remaining;
  gboolean enrol_cleaned;
  gboolean enrol_succeeded;
  gboolean enrol_existing;
} FpiDeviceFpc1264Oem;

typedef struct { FpDeviceClass parent; } FpiDeviceFpc1264OemClass;
G_DEFINE_TYPE (FpiDeviceFpc1264Oem, fpi_device_fpc1264_oem, FP_TYPE_DEVICE)

static void
clear_operation (FpiDeviceFpc1264Oem *self)
{
  g_clear_object (&self->child);
  g_clear_object (&self->enrol_output);
  g_clear_error (&self->enrol_error);
  if (self->database)
    g_unlink (self->database);
  if (self->request) g_unlink (self->request);
  if (self->result) g_unlink (self->result);
  g_clear_pointer (&self->request, g_free);
  g_clear_pointer (&self->result, g_free);
  g_clear_pointer (&self->gallery, g_ptr_array_unref);
  self->identifying = FALSE;
  if (self->directory)
    g_rmdir (self->directory);
  g_clear_pointer (&self->database, g_free);
  g_clear_object (&self->enrolled_print);
  g_clear_pointer (&self->directory, g_free);
  self->enrol_stages = 0;
  self->enrol_completed = 0;
  self->enrol_remaining = 0;
  self->enrol_cleaned = FALSE;
  self->enrol_succeeded = FALSE;
  self->enrol_existing = FALSE;
}

static void
probe (FpDevice *device)
{
  struct stat info;
  if (lstat ("/dev/fpc1020", &info) || !S_ISCHR (info.st_mode))
    fpi_device_probe_complete (device, NULL, NULL,
                              fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                                        "FPC1264 stable-base device is unavailable"));
  else
    fpi_device_probe_complete (device, "liuqin-fpc1264-oem", "FPC1264 OEM on liuqin", NULL);
}

static void
open_device (FpDevice *device)
{
  fpi_device_open_complete (device, geteuid () == 0 ? NULL :
                           fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
                                                     "The OEM runtime requires a privileged caller"));
}

static void
close_device (FpDevice *device)
{
  clear_operation ((FpiDeviceFpc1264Oem *) device);
  fpi_device_close_complete (device, NULL);
}

/* Accept only the private database exported after the provider's final
 * cleanup marker. fprintd will serialize and publish the resulting FpPrint. */
static gboolean
load_enrolled_database (FpiDeviceFpc1264Oem *self, GError **error)
{
  struct stat info;
  int fd = g_open (self->database, O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
  g_autofree guint8 *database = NULL;
  g_autoptr(GVariant) data = NULL;
  gsize length = 0;
  gboolean ok = FALSE;

  if (fd < 0 || fstat (fd, &info) || !S_ISREG (info.st_mode) ||
      info.st_uid != 0 || info.st_gid != 0 || (info.st_mode & 0777) != 0600 ||
      info.st_size != FPC_REFERENCE_SIZE)
    goto out;
  length = info.st_size;
  database = g_malloc (length);
  for (gsize offset = 0; offset < length;) {
    ssize_t count = read (fd, database + offset, length - offset);
    if (count < 0 && errno == EINTR)
      continue;
    if (count <= 0)
      goto out;
    offset += count;
  }
  if (length != FPC_REFERENCE_SIZE || memcmp (database, "FPCREF2", 8)) goto out;
  struct passwd *owner = getpwnam (fp_print_get_username (self->enrolled_print));
  if (!owner || !owner->pw_uid || fpc_le32 (database + 8) != owner->pw_uid ||
      fpc_le32 (database + 12) != (guint32) fp_print_get_finger (self->enrolled_print)) goto out;
  data = g_variant_ref_sink (g_variant_new ("(u@ay)", 2u,
             g_variant_new_fixed_array (G_VARIANT_TYPE_BYTE, database, length, 1)));
  fpi_print_set_type (self->enrolled_print, FPI_PRINT_RAW);
  g_object_set (self->enrolled_print, "fpi-data", data, NULL);
  ok = TRUE;
out:
  if (database)
    explicit_bzero (database, length);
  if (fd >= 0)
    close (fd);
  if (!ok)
    g_set_error_literal (error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_DATA_INVALID,
                         "The enrolled fingerprint database is incomplete or not private");
  return ok;
}

static void
enrol_finished (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FpDevice *device = user_data;
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  g_autoptr(GError) error = NULL;
  FpPrint *print = NULL;
  gboolean waited = g_subprocess_wait_finish (G_SUBPROCESS (source), result, &error);
  gboolean exited = waited && g_subprocess_get_if_exited (G_SUBPROCESS (source));
  gint status = exited ? g_subprocess_get_exit_status (G_SUBPROCESS (source)) : -1;
  gboolean cancelled = self->cancelled || fpi_device_action_is_cancelled (device);
  gboolean valid = exited && status == 0 && self->enrol_cleaned &&
                   self->enrol_succeeded && !self->enrol_error;

  if (cancelled && self->enrol_cleaned) {
    g_clear_error (&error);
    error = g_error_new_literal (G_IO_ERROR, G_IO_ERROR_CANCELLED,
                                 "Fingerprint enrollment cancelled after cleanup");
  } else if (self->enrol_error) {
    g_clear_error (&error);
    error = g_steal_pointer (&self->enrol_error);
  } else if (!valid || cancelled) {
    g_clear_error (&error);
    error = fpi_device_error_new_msg (self->enrol_existing ? FP_DEVICE_ERROR_DATA_FULL : FP_DEVICE_ERROR_GENERAL,
                                      self->enrol_existing ? "The selected finger is already enrolled or the hardware gallery is full" :
                                      "OEM enrollment did not finish with confirmed cleanup");
  } else if (load_enrolled_database (self, &error)) {
    print = g_steal_pointer (&self->enrolled_print);
  }
  if (!print && (!cancelled || !self->enrol_cleaned))
    g_warning ("OEM enroll runtime exit=%d cleanup_complete=%d enrolled=%d",
               status, self->enrol_cleaned, self->enrol_succeeded);
  clear_operation (self);
  fpi_device_report_finger_status (device, FP_FINGER_STATUS_NONE);
  fpi_device_enroll_complete (device, print, g_steal_pointer (&error));
  g_object_unref (device);
}

static void
enrol_retry (FpDevice *device, FpDeviceRetry retry)
{
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  if (!self->cancelled)
    fpi_device_enroll_progress (device, self->enrol_completed, NULL,
                                fpi_device_retry_new (retry));
}

static void
enrol_status_line (FpDevice *device, const gchar *line)
{
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  gint status;
  guint remaining;
  gchar extra;
  const gchar *failures[] = {
    "native_enrol_runtime=FAILED ", "native_enrol=FAILED ",
    "pipeline_incomplete=", "end_enrol_not_submitted=",
    "enrol_authorization=TA_rejected", "finger_irq_failed=",
    "finger_lost_wait_exhausted=", "capture_wait_time_timeout=", "finger_state_timeout=",
    "finger_state_invalid=", NULL
  };

  /* The provider guarantees these status/phase fields contain no credential,
   * token or template payload. Do not forward other stdout/stderr lines. */
  for (guint i = 0; failures[i]; i++)
    if (g_str_has_prefix (line, failures[i])) {
      g_warning ("OEM enroll diagnostic: %s", line);
      break;
    }

  /* This provider-wide marker follows every started child's cleanup. A
   * credential-creation runtime's earlier cleanup is deliberately ignored. */
  if (g_str_equal (line, "native_enrol_cleanup=OK"))
    self->enrol_cleaned = TRUE;
  else if (g_str_equal (line, "native_enrol=OK finger_reference=2")) {
    self->enrol_succeeded = self->enrol_cleaned;
  }
  else if (g_str_equal (line, "native_enrol=REFUSED existing_template=1") ||
           g_str_equal (line, "native_enrol=REFUSED gallery_full=1"))
    self->enrol_existing = TRUE;
  else if (g_str_equal (line, "capture_wait_time=1"))
    g_message ("OEM enroll phase=waiting-for-finger");
  else if (!self->cancelled &&
           sscanf (line, "enrol_progress_status=%d remaining=%u%c", &status, &remaining, &extra) == 2) {
    g_message ("OEM enroll sample status=%d remaining=%u", status, remaining);
    /* Positive OEM BIO_ENROL status means sampling continues. Zero is the
     * final sample and is only accepted through the final database result. */
    if (status < 0 || remaining >= 40) {
      enrol_retry (device, FP_DEVICE_RETRY_GENERAL);
      return;
    }
    if (!self->enrol_stages) {
      self->enrol_stages = remaining + 1;
      self->enrol_remaining = remaining + 1;
      fpi_device_set_nr_enroll_stages (device, self->enrol_stages);
    }
    if (remaining >= self->enrol_remaining) {
      enrol_retry (device, FP_DEVICE_RETRY_GENERAL);
      return;
    }
    self->enrol_remaining = remaining;
    /* Reserve completion for successful export/cleanup. A decreasing count
     * at this final boundary is progress, not a rejected sample. */
    gint completed = CLAMP (self->enrol_stages - (gint) remaining, 0, self->enrol_stages - 1);
    /* fprintd emits one EnrollStatus per callback, so publish every newly
     * completed stage even when the OEM remaining count drops by several. */
    while (!self->cancelled && self->enrol_completed < completed)
      fpi_device_enroll_progress (device, ++self->enrol_completed, NULL, NULL);
  } else if (!self->cancelled && g_str_has_prefix (line, "capture_rejected=")) {
    if (sscanf (line, "capture_rejected=%d", &status) == 1)
      g_message ("OEM enroll capture_rejected=%d", status);
    enrol_retry (device, FP_DEVICE_RETRY_GENERAL);
  } else if (!self->cancelled && g_str_has_prefix (line, "READY lift finger")) {
    g_message ("OEM enroll phase=lift-ready");
    fpi_device_report_finger_status (device, FP_FINGER_STATUS_PRESENT);
  } else if (!self->cancelled && g_str_has_prefix (line, "READY enrol attempt=")) {
    g_message ("OEM enroll phase=down-setup");
    fpi_device_report_finger_status (device, FP_FINGER_STATUS_NEEDED);
  } else if (!self->cancelled && g_str_has_prefix (line, "READY finger_irq_armed=down"))
    g_message ("OEM enroll phase=down-ready");
}

static void enrol_read_line (GObject *source, GAsyncResult *result, gpointer user_data);

static void
enrol_read_next (FpDevice *device)
{
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  /* Never cancel the read/wait: even a cancelled enrollment owns the sensor
   * until the provider has unwound its TEE clients and exited. */
  g_data_input_stream_read_line_async (self->enrol_output, G_PRIORITY_DEFAULT, NULL,
                                      enrol_read_line, device);
}

static void
enrol_read_line (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FpDevice *device = user_data;
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  g_autoptr(GError) error = NULL;
  gsize length;
  g_autofree gchar *line = g_data_input_stream_read_line_finish_utf8 (G_DATA_INPUT_STREAM (source),
                                                                    result, &length, &error);
  if (line) {
    if (length <= 4096)
      enrol_status_line (device, line);
    enrol_read_next (device);
    return;
  }
  if (error) {
    self->enrol_error = g_steal_pointer (&error);
    g_subprocess_send_signal (self->child, SIGTERM);
  }
  g_subprocess_wait_async (self->child, NULL, enrol_finished, device);
}

static void
enrol (FpDevice *device)
{
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  FpPrint *print;
  const gchar *bundle = g_getenv ("LIUQIN_FPC_OEM_RUNTIME");
  const gchar *username;
  struct stat info;
  g_autofree gchar *provider = NULL;
  g_autofree gchar *finger = NULL;
  g_autoptr(GError) error = NULL;
  g_autoptr(GSubprocessLauncher) launcher = NULL;

  fpi_device_get_enroll_data (device, &print);
  username = fp_print_get_username (print);
  if (!username || !*username || !FP_FINGER_IS_VALID (fp_print_get_finger (print))) {
    error = fpi_device_error_new (FP_DEVICE_ERROR_DATA_INVALID);
    goto failed;
  }
  if (!bundle || !g_path_is_absolute (bundle) || lstat (bundle, &info) ||
      !S_ISDIR (info.st_mode) || info.st_uid != 0 || (info.st_mode & 0022)) {
    error = fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL, "Supply a trusted OEM runtime directory");
    goto failed;
  }
  provider = g_build_filename (bundle, "native_enrol.py", NULL);
  if (lstat (provider, &info) || !S_ISREG (info.st_mode) || info.st_uid != 0 || (info.st_mode & 0022)) {
    error = fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL, "Native enrollment provider is missing");
    goto failed;
  }
  self->directory = g_dir_make_tmp ("liuqin-fpc-enrol-XXXXXX", &error);
  if (!self->directory)
    goto failed;
  self->database = g_build_filename (self->directory, "finger.ref", NULL);
  self->cancelled = FALSE;
  self->enrolled_print = g_object_ref (print);
  fpi_device_set_nr_enroll_stages (device, 20);
  launcher = g_subprocess_launcher_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_MERGE);
  g_subprocess_launcher_setenv (launcher, "PYTHONUNBUFFERED", "1", TRUE);
  finger = g_strdup_printf ("%u", fp_print_get_finger (print));
  self->child = g_subprocess_launcher_spawn (launcher, &error, "/usr/bin/python3", provider,
                                            "--enrol", username, finger, self->database, NULL);
  if (!self->child)
    goto failed;
  self->enrol_output = g_data_input_stream_new (g_subprocess_get_stdout_pipe (self->child));
  fpi_device_report_finger_status (device, FP_FINGER_STATUS_NEEDED);
  enrol_read_next (g_object_ref (device));
  return;
failed:
  clear_operation (self);
  fpi_device_enroll_complete (device, NULL, g_steal_pointer (&error));
}

/* fprintd supplies only the authorized account's gallery. No template or
 * secure ID is passed through logs; the helper returns an index into this exact
 * retained array only after hardware cleanup and durable adaptive storage. */
static void
gallery_finished (GObject *source, GAsyncResult *result, gpointer user_data)
{
  FpDevice *device = user_data;
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  gboolean identifying = self->identifying;
  g_autoptr(GError) error = NULL;
  g_autofree gchar *output = NULL, *diagnostic = NULL;
  g_autoptr(FpPrint) match = NULL;
  gboolean done = g_subprocess_communicate_utf8_finish (G_SUBPROCESS (source), result, &output, &diagnostic, &error);
  gboolean cancelled = self->cancelled || fpi_device_action_is_cancelled (device);
  gboolean valid = done && g_subprocess_get_if_exited (G_SUBPROCESS (source)) &&
    g_subprocess_get_exit_status (G_SUBPROCESS (source)) == 0 && output &&
    strstr (output, "gallery_match=OK cleanup_complete=1\n");
  guint32 status = 0, index = G_MAXUINT32;
  if (valid) {
    guint8 data[FPC_RESULT_SIZE];
    struct stat info;
    int fd = g_open (self->result, O_RDONLY | O_CLOEXEC | O_NOFOLLOW, 0);
    valid = fd >= 0 && !fstat (fd, &info) && S_ISREG (info.st_mode) && !info.st_uid &&
            (info.st_mode & 0777) == 0600 && info.st_size == FPC_RESULT_SIZE &&
            read (fd, data, sizeof data) == sizeof data && !memcmp (data, "FPCRES2", 8);
    if (fd >= 0) close (fd);
    if (valid) {
      status = fpc_le32 (data + 8); index = fpc_le32 (data + 12);
      valid = (status == 1 && index < self->gallery->len) ||
              ((status == 0 || status == 2) && index == G_MAXUINT32);
      if (valid && status == 1) match = g_object_ref (g_ptr_array_index (self->gallery, index));
    }
  }
  if (!valid && !cancelled && !error)
    error = fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL, "OEM gallery identification did not complete");
  clear_operation (self);
  fpi_device_report_finger_status (device, FP_FINGER_STATUS_NONE);
  if (cancelled) {
    g_clear_error (&error);
    error = g_error_new_literal (G_IO_ERROR, G_IO_ERROR_CANCELLED, "Fingerprint identification cancelled");
  }
  if (error) {
    if (identifying) fpi_device_identify_complete (device, g_steal_pointer (&error));
    else fpi_device_verify_complete (device, g_steal_pointer (&error));
  } else if (identifying) {
    fpi_device_identify_report (device, match, NULL,
      status == 2 ? fpi_device_retry_new (FP_DEVICE_RETRY_GENERAL) : NULL);
    fpi_device_identify_complete (device, NULL);
  } else {
    fpi_device_verify_report (device, status == 2 ? FPI_MATCH_ERROR : match ? FPI_MATCH_SUCCESS : FPI_MATCH_FAIL,
      NULL, status == 2 ? fpi_device_retry_new (FP_DEVICE_RETRY_GENERAL) : NULL);
    fpi_device_verify_complete (device, NULL);
  }
  g_object_unref (device);
}

static void
gallery_start (FpDevice *device, GPtrArray *prints, gboolean identifying)
{
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  g_autoptr(GError) error = NULL;
  g_autoptr(GByteArray) request = g_byte_array_new ();
  const gchar *bundle = g_getenv ("LIUQIN_FPC_OEM_RUNTIME");
  const gchar *username = NULL;
  g_autofree gchar *helper = NULL;
  struct stat info;
  guint32 uid = 0;
  guint8 header[20] = "FPCREQ2";
  if (!prints || prints->len > FPC_GALLERY_MAX) goto invalid;
  if (!prints->len && identifying) {
    fpi_device_identify_report (device, NULL, NULL, NULL);
    fpi_device_identify_complete (device, NULL); return;
  }
  if (!prints->len || !bundle || !g_path_is_absolute (bundle) || lstat (bundle, &info) ||
      !S_ISDIR (info.st_mode) || info.st_uid || (info.st_mode & 0022)) goto invalid;
  helper = g_build_filename (bundle, "gallery_match.py", NULL);
  if (lstat (helper, &info) || !S_ISREG (info.st_mode) || info.st_uid || (info.st_mode & 0022)) goto invalid;
  const gchar *dependencies[] = {"gallery_state.py", "native_enrol.py", "oem_runtime.py",
                                  "fpc-oem-print", "fpc_build_info"};
  for (guint i = 0; i < G_N_ELEMENTS (dependencies); i++) {
    g_autofree gchar *path = g_build_filename (bundle, dependencies[i], NULL);
    if (lstat (path, &info) || !S_ISREG (info.st_mode) || info.st_uid || (info.st_mode & 0022)) goto invalid;
  }
  self->gallery = g_ptr_array_new_with_free_func (g_object_unref);
  for (guint i = 0; i < prints->len; i++) {
    FpPrint *print = g_ptr_array_index (prints, i);
    g_autoptr(GVariant) data = NULL, bytes = NULL;
    guint32 version;
    gsize length;
    const gchar *owner = fp_print_get_username (print);
    if (!owner || !*owner || !FP_FINGER_IS_VALID (fp_print_get_finger (print))) goto invalid;
    if (!i) {
      struct passwd *user = getpwnam (owner);
      if (!user || !user->pw_uid || user->pw_uid >= 0x10000000) goto invalid;
      username = owner; uid = user->pw_uid;
      fpc_put32 (header + 8, uid); fpc_put32 (header + 12, prints->len);
      fpc_put32 (header + 16, strlen (username));
      g_byte_array_append (request, header, sizeof header);
      g_byte_array_append (request, (const guint8 *) username, strlen (username));
    } else if (g_strcmp0 (username, owner)) goto invalid;
    if (g_strcmp0 (fp_print_get_driver (print), "fpc1264_oem") ||
        g_strcmp0 (fp_print_get_device_id (print), "liuqin-fpc1264-oem")) goto invalid;
    g_object_get (print, "fpi-data", &data, NULL);
    if (!data || !g_variant_is_of_type (data, G_VARIANT_TYPE ("(uay)"))) goto invalid;
    g_variant_get (data, "(u@ay)", &version, &bytes);
    const guint8 *payload = g_variant_get_fixed_array (bytes, &length, 1);
    if (version != 2 || !length || length > FPC_DATABASE_MAX ||
        request->len + 12 + length > FPC_DATABASE_MAX + 16384) goto invalid;
    if (version == 2 && (length != FPC_REFERENCE_SIZE || memcmp (payload, "FPCREF2", 8) ||
        fpc_le32 (payload + 8) != uid || fpc_le32 (payload + 12) != (guint32) fp_print_get_finger (print))) goto invalid;
    guint8 item[12]; fpc_put32 (item, fp_print_get_finger (print)); fpc_put32 (item + 4, version);
    fpc_put32 (item + 8, length); g_byte_array_append (request, item, sizeof item);
    g_byte_array_append (request, payload, length);
    g_ptr_array_add (self->gallery, g_object_ref (print));
  }
  self->identifying = identifying; self->cancelled = FALSE;
  self->directory = g_dir_make_tmp ("liuqin-fpc-gallery-XXXXXX", &error);
  if (!self->directory) goto failed;
  self->request = g_build_filename (self->directory, "request", NULL);
  self->result = g_build_filename (self->directory, "result", NULL);
  int fd = g_open (self->request, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
  if (fd < 0) goto invalid;
  gboolean written = TRUE;
  for (gsize offset = 0; offset < request->len;) {
    ssize_t count = write (fd, request->data + offset, request->len - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) { written = FALSE; break; }
    offset += count;
  }
  if (close (fd)) written = FALSE;
  if (!written) goto invalid;
  self->child = g_subprocess_new (G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE,
      &error, "/usr/bin/python3", helper, "--request", self->request, "--result", self->result, NULL);
  if (!self->child) goto failed;
  fpi_device_report_finger_status (device, FP_FINGER_STATUS_NEEDED);
  g_subprocess_communicate_utf8_async (self->child, NULL, NULL, gallery_finished, g_object_ref (device));
  return;
invalid:
  if (!error) error = fpi_device_error_new_msg (FP_DEVICE_ERROR_DATA_INVALID, "Invalid account fingerprint gallery");
failed:
  clear_operation (self);
  if (identifying) fpi_device_identify_complete (device, g_steal_pointer (&error));
  else fpi_device_verify_complete (device, g_steal_pointer (&error));
}

static void
identify (FpDevice *device)
{
  GPtrArray *prints = NULL;
  fpi_device_get_identify_data (device, &prints);
  gallery_start (device, prints, TRUE);
}

static void
verify (FpDevice *device)
{
  FpPrint *print = NULL;
  fpi_device_get_verify_data (device, &print);
  g_autoptr(GPtrArray) prints = g_ptr_array_new ();
  g_ptr_array_add (prints, print);
  gallery_start (device, prints, FALSE);
}

static void
cancel (FpDevice *device)
{
  FpiDeviceFpc1264Oem *self = (FpiDeviceFpc1264Oem *) device;
  self->cancelled = TRUE;
  if (self->child)
    g_subprocess_send_signal (self->child, SIGTERM);
}

static void
fpi_device_fpc1264_oem_init (FpiDeviceFpc1264Oem *self)
{
  (void) self;
}

static void
fpi_device_fpc1264_oem_class_init (FpiDeviceFpc1264OemClass *klass)
{
  static const FpIdEntry devices[] = {
    { .virtual_envvar = "FP_LIUQIN_FPC1264_OEM_ENABLE" }, { .pid = 0 }
  };
  FpDeviceClass *device = FP_DEVICE_CLASS (klass);
  device->id = "fpc1264_oem";
  device->full_name = "FPC1264 OEM on liuqin";
  device->type = FP_DEVICE_TYPE_VIRTUAL;
  device->scan_type = FP_SCAN_TYPE_PRESS;
  device->id_table = devices;
  device->temp_hot_seconds = -1;
  device->nr_enroll_stages = 20;
  device->probe = probe;
  device->open = open_device;
  device->close = close_device;
  device->enroll = enrol;
  device->verify = verify;
  device->identify = identify;
  device->cancel = cancel;
  fpi_device_class_auto_initialize_features (device);
}

G_MODULE_EXPORT GType
fpi_tod_shared_driver_get_type (void)
{
  return fpi_device_fpc1264_oem_get_type ();
}
