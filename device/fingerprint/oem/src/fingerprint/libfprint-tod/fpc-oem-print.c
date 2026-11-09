/* SPDX-License-Identifier: LGPL-2.1-or-later
 * Opaque database to FpPrint serialization. Caller owns user authentication,
 * binary FDs and publication; this never registers a print or changes PAM.
 */
#include <fprint.h>
#include <fpi-print.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "../protocol/fpc_gallery.h"

static int
descriptor (const char *argument)
{
  char *end;
  errno = 0;
  long value = strtol (argument, &end, 10);
  return errno || end == argument || *end || value < 3 || value > INT_MAX ? -1 : (int) value;
}

/* Decode private host prints without enumerating or opening a device. */
static int
decode_print (const char *username, const char *input_text, const char *output_text)
{
  int input = descriptor (input_text), output = descriptor (output_text);
  struct stat in, out;
  struct passwd *user = getpwnam (username);
  g_autofree guint8 *serialized = NULL;
  g_autoptr(FpPrint) print = NULL;
  g_autoptr(GVariant) data = NULL;
  g_autoptr(GVariant) bytes = NULL;
  g_autoptr(GError) error = NULL;
  guint32 version;
  gsize length;
  if (!user || input < 0 || output < 0 || input == output ||
      fstat (input, &in) || fstat (output, &out) ||
      !S_ISREG (in.st_mode) || !S_ISREG (out.st_mode) || in.st_uid || out.st_uid ||
      (in.st_mode & 0777) != 0600 || (out.st_mode & 0777) != 0600 ||
      in.st_size <= 0 || in.st_size > FPC_DATABASE_MAX + 1024 * 1024 || out.st_size ||
      (in.st_dev == out.st_dev && in.st_ino == out.st_ino) ||
      lseek (input, 0, SEEK_SET) < 0 || lseek (output, 0, SEEK_SET) < 0) return 2;
  serialized = g_malloc (in.st_size);
  for (gsize offset = 0; offset < (gsize) in.st_size;) {
    ssize_t count = read (input, serialized + offset, in.st_size - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return 1;
    offset += count;
  }
  print = fp_print_deserialize (serialized, in.st_size, &error);
  explicit_bzero (serialized, in.st_size);
  if (!print || g_strcmp0 (fp_print_get_username (print), username) ||
      g_strcmp0 (fp_print_get_driver (print), "fpc1264_oem") ||
      g_strcmp0 (fp_print_get_device_id (print), "liuqin-fpc1264-oem") ||
      !FP_FINGER_IS_VALID (fp_print_get_finger (print))) return 1;
  g_object_get (print, "fpi-data", &data, NULL);
  if (!data || !g_variant_is_of_type (data, G_VARIANT_TYPE ("(uay)"))) return 1;
  g_variant_get (data, "(u@ay)", &version, &bytes);
  const guint8 *payload = g_variant_get_fixed_array (bytes, &length, 1);
  if (version != 2 || !length || length > FPC_DATABASE_MAX) return 1;
  if (version == 2 && (length != FPC_REFERENCE_SIZE || memcmp (payload, "FPCREF2", 8) ||
      fpc_le32 (payload + 8) != user->pw_uid ||
      fpc_le32 (payload + 12) != (guint32) fp_print_get_finger (print))) return 1;
  guint8 header[8]; fpc_put32 (header, version);
  fpc_put32 (header + 4, fp_print_get_finger (print));
  if (write (output, header, sizeof header) != sizeof header) return 1;
  for (gsize offset = 0; offset < length;) {
    ssize_t count = write (output, payload + offset, length - offset);
    if (count < 0 && errno == EINTR) continue;
    if (count <= 0) return 1;
    offset += count;
  }
  return fsync (output) ? 1 : 0;
}

int
main (int argc, char **argv)
{
  if (argc == 5 && !strcmp (argv[1], "--decode") && geteuid () == 0)
    return decode_print (argv[2], argv[3], argv[4]);
  g_printerr ("usage (root): fpc-oem-print --decode USERNAME PRINT_FD DATA_FD\n");
  return 2;
}
