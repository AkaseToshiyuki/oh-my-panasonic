/* Return the local foreground graphical session ID only if all its gates match.
 * This helper has no privileges of its own and does not authenticate a user. */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <systemd/sd-bus.h>
#include <systemd/sd-login.h>

int main(int argc, char **argv)
{
    if (argc != 3 || (strcmp(argv[2], "0") && strcmp(argv[2], "1")))
        return 1;
    char *end;
    errno = 0;
    unsigned long expected = strtoul(argv[1], &end, 10);
    if (errno || !*argv[1] || *end || expected != (uid_t)expected)
        return 1;

    char *id = NULL, *seat = NULL, *type = NULL, *class = NULL, *path = NULL;
    sd_bus *bus = NULL;
    uid_t owner, session_uid;
    int locked, status = 1;
    if (sd_seat_get_active("seat0", &id, &owner) < 0 || owner != expected ||
        sd_session_get_uid(id, &session_uid) < 0 || session_uid != owner ||
        sd_session_get_seat(id, &seat) < 0 || strcmp(seat, "seat0") ||
        sd_session_get_class(id, &class) < 0 || strcmp(class, "user") ||
        sd_session_get_type(id, &type) < 0 || (strcmp(type, "wayland") && strcmp(type, "x11")) ||
        sd_session_is_remote(id) != 0 || sd_session_is_active(id) <= 0)
        goto done;
    if (sd_bus_open_system(&bus) < 0 ||
        sd_bus_set_method_call_timeout(bus, 2000000) < 0 ||
        sd_bus_path_encode("/org/freedesktop/login1/session", id, &path) < 0 ||
        sd_bus_get_property_trivial(bus, "org.freedesktop.login1", path,
            "org.freedesktop.login1.Session", "LockedHint", NULL, 'b', &locked) < 0) {
        status = 2;  /* logind transport failure: caller can retry via loginctl */
        goto done;
    }
    if (!!locked == (argv[2][0] == '1') && sd_session_is_active(id) > 0) {
        puts(id);
        status = 0;
    }
done:
    sd_bus_unref(bus);
    free(path);
    free(class);
    free(type);
    free(seat);
    free(id);
    return status;
}
