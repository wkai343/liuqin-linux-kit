/* SPDX-License-Identifier: MIT
 * Real libfprint Identify/Verify/Enroll API against synthetic helpers only.
 */
#include "../device/fingerprint/oem/src/fingerprint/libfprint-tod/fpc1264-oem.c"

typedef struct {
  GMainLoop *loop;
  FpDevice *device;
  GCancellable *cancel;
  GError *error;
  FpPrint *match;
  gboolean ok;
  gint64 cancelled, finished;
} Fixture;
static gchar *bundle;

static void probe_fixture (FpDevice *device)
{
  fpi_device_probe_complete (device, "liuqin-fpc1264-oem", "Synthetic gallery", NULL);
}
static void initialized (GObject *source, GAsyncResult *result, gpointer data)
{
  Fixture *f = data;
  f->device = FP_DEVICE (g_async_initable_new_finish (G_ASYNC_INITABLE (source), result, &f->error));
  g_main_loop_quit (f->loop);
}
static void identified (GObject *source, GAsyncResult *result, gpointer data)
{
  Fixture *f = data;
  f->ok = fp_device_identify_finish (FP_DEVICE (source), result, &f->match, NULL, &f->error);
  f->finished = g_get_monotonic_time ();
  g_main_loop_quit (f->loop);
}
static gboolean cancel_when_ready (gpointer data)
{
  Fixture *f = data;
  g_autofree gchar *path = g_build_filename (bundle, "ready", NULL);
  if (!g_file_test (path, G_FILE_TEST_EXISTS)) return G_SOURCE_CONTINUE;
  f->cancelled = g_get_monotonic_time (); g_cancellable_cancel (f->cancel);
  return G_SOURCE_REMOVE;
}
static gboolean timeout (gpointer data)
{
  (void) data; g_error ("Synthetic gallery API timed out"); return G_SOURCE_REMOVE;
}
static FpPrint *make_print (FpDevice *device, FpFinger finger, const gchar *username, gboolean legacy)
{
  FpPrint *print = g_object_ref_sink (fp_print_new (device));
  fpi_print_set_type (print, FPI_PRINT_RAW); fp_print_set_username (print, username);
  fp_print_set_finger (print, finger);
  guint8 payload[FPC_REFERENCE_SIZE] = "FPCREF2";
  fpc_put32 (payload + 8, getpwnam (username)->pw_uid); fpc_put32 (payload + 12, finger);
  memset (payload + 16, finger, 32);
  GVariant *bytes = g_variant_new_fixed_array (G_VARIANT_TYPE_BYTE,
                       legacy ? (guint8 *) "opaque legacy database" : payload,
                       legacy ? strlen ("opaque legacy database") : sizeof payload, 1);
  g_object_set (print, "fpi-data", g_variant_new ("(u@ay)", legacy ? 1u : 2u, bytes), NULL);
  return print;
}
static void decode_roundtrip (FpPrint *print)
{
  const gchar *decoder = g_getenv ("FPC_PRINT_DECODER");
  g_autoptr(GError) error = NULL;
  g_autofree guint8 *data = NULL;
  gsize length;
  g_assert_true (fp_print_serialize (print, &data, &length, &error));
  g_autofree gchar *input_path = NULL, *output_path = NULL;
  int input = g_file_open_tmp ("fpc-decode-in-XXXXXX", &input_path, &error);
  int output = g_file_open_tmp ("fpc-decode-out-XXXXXX", &output_path, &error);
  g_assert_no_error (error); g_assert_cmpint (fchmod (input, 0600), ==, 0);
  g_assert_cmpint (fchmod (output, 0600), ==, 0);
  g_assert_cmpint (write (input, data, length), ==, (ssize_t) length);
  g_autoptr(GSubprocessLauncher) launcher = g_subprocess_launcher_new (G_SUBPROCESS_FLAGS_STDERR_PIPE);
  g_subprocess_launcher_take_fd (launcher, dup (input), 3);
  g_subprocess_launcher_take_fd (launcher, dup (output), 4);
  g_autoptr(GSubprocess) process = g_subprocess_launcher_spawn (launcher, &error, decoder,
     "--decode", fp_print_get_username (print), "3", "4", NULL);
  g_assert_no_error (error); g_assert_true (g_subprocess_wait_check (process, NULL, &error));
  g_assert_no_error (error);
  guint8 header[8]; g_assert_cmpint (lseek (output, 0, SEEK_SET), ==, 0);
  g_assert_cmpint (read (output, header, sizeof header), ==, sizeof header);
  g_assert_cmpuint (fpc_le32 (header + 4), ==, fp_print_get_finger (print));
  g_assert_true (fpc_le32 (header) == 1 || fpc_le32 (header) == 2);
  close (input); close (output); g_unlink (input_path); g_unlink (output_path);
}
static void exercise (gconstpointer data)
{
  const gchar *mode = data;
  Fixture f = {0}; f.loop = g_main_loop_new (NULL, FALSE); f.cancel = g_cancellable_new ();
  FpDeviceClass *klass = g_type_class_ref (fpi_device_fpc1264_oem_get_type ());
  klass->probe = probe_fixture;
  g_async_initable_new_async (fpi_device_fpc1264_oem_get_type (), G_PRIORITY_DEFAULT, NULL,
                              initialized, &f, "fpi-environ", "test", NULL);
  g_main_loop_run (f.loop); g_assert_no_error (f.error);
  g_assert_true (fp_device_open_sync (f.device, NULL, &f.error)); g_assert_no_error (f.error);
  g_assert_true (fp_device_has_feature (f.device, FP_DEVICE_FEATURE_IDENTIFY));
  const gchar *username = "nobody"; g_assert_nonnull (getpwnam (username));
  g_autoptr(GPtrArray) prints = g_ptr_array_new_with_free_func (g_object_unref);
  if (!g_str_equal (mode, "empty")) {
    g_ptr_array_add (prints, make_print (f.device, FP_FINGER_LEFT_INDEX, username, g_str_equal (mode, "old-format")));
    g_ptr_array_add (prints, make_print (f.device, FP_FINGER_RIGHT_INDEX,
                                      g_str_equal (mode, "mixed-users") ? "root" : username, FALSE));
  }
  g_setenv ("FPC_GALLERY_TEST_CASE", mode, TRUE);
  if (g_str_equal (mode, "enroll")) {
    g_autoptr(FpPrint) candidate = g_object_ref_sink (fp_print_new (f.device));
    fp_print_set_username (candidate, username); fp_print_set_finger (candidate, FP_FINGER_LEFT_INDEX);
    g_autoptr(FpPrint) enrolled = fp_device_enroll_sync (f.device, candidate, NULL, NULL, NULL, &f.error);
    g_assert_no_error (f.error); g_assert_nonnull (enrolled);
    g_autoptr(GVariant) v = NULL, bytes = NULL; guint32 version;
    g_object_get (enrolled, "fpi-data", &v, NULL); g_variant_get (v, "(u@ay)", &version, &bytes);
    g_assert_cmpuint (version, ==, 2); decode_roundtrip (enrolled);
  } else if (g_str_has_prefix (mode, "verify-")) {
    g_setenv ("FPC_GALLERY_TEST_CASE", g_str_equal (mode, "verify-match") ? "first" : "miss", TRUE);
    gboolean match = FALSE;
    g_assert_true (fp_device_verify_sync (f.device, g_ptr_array_index (prints, 0), NULL, NULL, NULL,
                                         &match, NULL, &f.error)); g_assert_no_error (f.error);
    g_assert_cmpint (match, ==, g_str_equal (mode, "verify-match"));
  } else {
    if (g_str_equal (mode, "cancel")) g_timeout_add (5, cancel_when_ready, &f);
    guint deadline = g_timeout_add_seconds (10, timeout, NULL);
    fp_device_identify (f.device, prints, f.cancel, NULL, NULL, NULL, identified, &f);
    g_main_loop_run (f.loop); g_source_remove (deadline);
    if (g_str_equal (mode, "first") || g_str_equal (mode, "second")) {
      g_assert_true (f.ok); g_assert_no_error (f.error);
      g_assert_true (f.match == g_ptr_array_index (prints, g_str_equal (mode, "second") ? 1 : 0));
      decode_roundtrip (f.match);
    } else if (g_str_equal (mode, "miss") || g_str_equal (mode, "empty")) {
      g_assert_true (f.ok); g_assert_no_error (f.error); g_assert_null (f.match);
    } else {
      g_assert_false (f.ok); g_assert_null (f.match);
      if (g_str_equal (mode, "retry")) g_assert_error (f.error, FP_DEVICE_RETRY, FP_DEVICE_RETRY_GENERAL);
      else if ((g_str_equal (mode, "mixed-users") || g_str_equal (mode, "old-format"))) g_assert_error (f.error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_DATA_INVALID);
      else if (g_str_equal (mode, "cancel")) {
        g_assert_error (f.error, G_IO_ERROR, G_IO_ERROR_CANCELLED);
        g_assert_cmpint (f.finished - f.cancelled, >=, 190000);
      } else g_assert_error (f.error, FP_DEVICE_ERROR, FP_DEVICE_ERROR_GENERAL);
    }
  }
  g_clear_error (&f.error); g_clear_object (&f.match);
  g_assert_true (fp_device_close_sync (f.device, NULL, &f.error)); g_assert_no_error (f.error);
  g_clear_object (&f.device); g_clear_object (&f.cancel); g_main_loop_unref (f.loop); g_type_class_unref (klass);
  g_autofree gchar *ready = g_build_filename (bundle, "ready", NULL); g_unlink (ready);
}
int main (int argc, char **argv)
{
  g_assert_cmpuint (geteuid (), ==, 0); g_test_init (&argc, &argv, NULL);
  g_log_set_always_fatal (G_LOG_LEVEL_ERROR | G_LOG_LEVEL_CRITICAL);
  g_autoptr(GError) error = NULL; gchar *fixture = NULL; gsize length;
  g_assert_true (g_file_get_contents (g_getenv ("FPC_GALLERY_FIXTURE"), &fixture, &length, &error));
  bundle = g_dir_make_tmp ("fpc-gallery-api-XXXXXX", &error); g_assert_no_error (error);
  const gchar *names[] = {"gallery_match.py", "native_enrol.py", "gallery_state.py", "oem_runtime.py", "fpc-oem-print", "fpc_build_info"};
  for (guint i = 0; i < G_N_ELEMENTS (names); i++) {
    g_autofree gchar *path = g_build_filename (bundle, names[i], NULL);
    g_assert_true (g_file_set_contents (path, fixture, length, &error)); g_assert_no_error (error);
    g_assert_cmpint (g_chmod (path, 0600), ==, 0);
  }
  g_setenv ("LIUQIN_FPC_OEM_RUNTIME", bundle, TRUE);
  const gchar *cases[] = {"first", "second", "old-format", "miss", "empty", "retry", "bad-index",
    "bad-magic", "partial", "public-result", "missing-marker", "mixed-users", "cancel",
    "verify-match", "verify-miss", "enroll"};
  for (guint i = 0; i < G_N_ELEMENTS (cases); i++) {
    g_autofree gchar *name = g_strconcat ("/gallery-api/", cases[i], NULL);
    g_test_add_data_func (name, cases[i], exercise);
  }
  int result = g_test_run ();
  for (guint i = 0; i < G_N_ELEMENTS (names); i++) {
    g_autofree gchar *path = g_build_filename (bundle, names[i], NULL); g_unlink (path);
  }
  g_rmdir (bundle); g_free (bundle); g_free (fixture); return result;
}
