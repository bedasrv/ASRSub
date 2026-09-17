#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#if !defined(ASRSUB_BUNDLE_SIGNER) && !defined(ASRSUB_APPROVAL_SIGNER)
#error "select exactly one ASRSub signer role"
#endif
#if defined(ASRSUB_BUNDLE_SIGNER) && defined(ASRSUB_APPROVAL_SIGNER)
#error "select only one ASRSub signer role"
#endif

#if defined(ASRSUB_BUNDLE_SIGNER)
#define ROLE_TOKEN "asrsub-bundle-signing-key"
#define KEY_SOURCE "/etc/asrsub/signing/bundle-signing-key.pem"
#define KEY_NAME "bundle-signing-key.pem"
#define TARGET_FD 3
/* Keep this array byte-for-byte aligned with the bundle policy command. */
static const char *const child_argv[] = {
    "/usr/bin/python3",
    "tools/asrsub-env",
    "--pass-fd",
    "3",
    "/usr/bin/python3",
    "tools/package_bundle.py",
    "--production",
    "--runtime-source-root",
    "release/runtime",
    "--systemd-source-root",
    "release/systemd",
    "--output-root",
    "release/asrsub-runtime-bundle",
    "--manifest-output",
    "release/bundle-manifest.json",
    "--signature-output",
    "release/bundle-manifest.sig",
    "--release-sha-from-git",
    "--key-fd",
    "3",
    NULL,
};
#else
#define ROLE_TOKEN "asrsub-approval-key"
#define KEY_SOURCE "/etc/asrsub/signing/approval-key.pem"
#define KEY_NAME "approval-key.pem"
#define TARGET_FD 4
/* Keep this array byte-for-byte aligned with the approval policy command. */
static const char *const child_argv[] = {
    "/usr/bin/python3",
    "tools/asrsub-env",
    "--pass-fd",
    "4",
    "/usr/bin/python3",
    "tools/create_approval.py",
    "--production",
    "--canonical-approval-bytes",
    "release/approval-canonical.json",
    "--approval-manifest",
    "release/approval.json",
    "--approval-signature",
    "release/approval.sig",
    "--release-sha-from-git",
    "--approval-key-fd",
    "4",
    NULL,
};
#endif

static const char key_directory[] = "/etc/asrsub/signing";
static char *const clean_environment[] = {
    (char *)"LANG=C",
    (char *)"LC_ALL=C",
    (char *)"PATH=/usr/bin:/bin",
    NULL,
};

static void write_message(const char *message, size_t length)
{
    size_t offset = 0U;

    while (offset < length) {
        ssize_t written = write(STDERR_FILENO, message + offset, length - offset);

        if (written <= 0) {
            return;
        }
        offset += (size_t)written;
    }
}

static int reject_invocation(void)
{
    static const char message[] = "asrsub-signer: invocation rejected\n";
    write_message(message, sizeof(message) - 1U);
    return 64;
}

static int operation_failed(void)
{
    static const char message[] = "asrsub-signer: operation failed\n";
    write_message(message, sizeof(message) - 1U);
    return 126;
}

static size_t child_argc(void)
{
    size_t count = 0U;

    while (child_argv[count] != NULL) {
        ++count;
    }
    return count;
}

static int safe_absolute_path(const char *path)
{
    const char *cursor;

    if (path == NULL || path[0] != '/' || path[1] == '\0') {
        return -1;
    }
    if (strlen(path) >= PATH_MAX) {
        return -1;
    }

    cursor = path + 1;
    while (*cursor != '\0') {
        const char *start = cursor;
        size_t length;

        while (*cursor != '\0' && *cursor != '/') {
            ++cursor;
        }
        length = (size_t)(cursor - start);
        if (length == 1U && start[0] == '.') {
            return -1;
        }
        if (length == 2U && start[0] == '.' && start[1] == '.') {
            return -1;
        }
        if (*cursor == '/') {
            ++cursor;
        }
    }
    return 0;
}

static int safe_directory_fd(int fd)
{
    struct stat status;
    mode_t mode;

    if (fstat(fd, &status) < 0 || !S_ISDIR(status.st_mode)) {
        return -1;
    }
    if (status.st_uid != 0U && (status.st_mode & S_ISVTX) == 0U) {
        return -1;
    }
    mode = status.st_mode;
    if ((mode & (S_ISUID | S_ISGID)) != 0U ||
        ((mode & (S_IWGRP | S_IWOTH)) != 0U && (mode & S_ISVTX) == 0U)) {
        return -1;
    }
    return 0;
}

static int safe_final_directory_fd(int fd)
{
    struct stat status;

    if (fstat(fd, &status) < 0 || status.st_uid != 0U ||
        (status.st_mode & (S_ISUID | S_ISGID | S_IWGRP | S_IWOTH)) != 0U) {
        return -1;
    }
    return 0;
}

static int open_safe_directory(const char *path)
{
    int current;
    const char *cursor;

    if (safe_absolute_path(path) < 0) {
        return -1;
    }
    current = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (current < 0 || safe_directory_fd(current) < 0) {
        if (current >= 0) {
            (void)close(current);
        }
        return -1;
    }

    cursor = path + 1;
    while (*cursor != '\0') {
        const char *start = cursor;
        size_t length;
        char component[NAME_MAX + 1U];
        int next;

        while (*cursor != '\0' && *cursor != '/') {
            ++cursor;
        }
        length = (size_t)(cursor - start);
        if (length == 0U) {
            if (*cursor == '/') {
                ++cursor;
            }
            continue;
        }
        if (length > NAME_MAX) {
            (void)close(current);
            return -1;
        }
        (void)memcpy(component, start, length);
        component[length] = '\0';
        next = openat(current, component, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (next < 0 || safe_directory_fd(next) < 0) {
            if (next >= 0) {
                (void)close(next);
            }
            (void)close(current);
            return -1;
        }
        (void)close(current);
        current = next;
        if (*cursor == '/') {
            ++cursor;
        }
    }
    if (safe_final_directory_fd(current) < 0) {
        (void)close(current);
        return -1;
    }
    return current;
}

static int open_signing_key(void)
{
    int directory_fd;
    int key_fd;
    struct stat status;

    directory_fd = open_safe_directory(key_directory);
    if (directory_fd < 0) {
        return -1;
    }
    key_fd = openat(directory_fd, KEY_NAME, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    (void)close(directory_fd);
    if (key_fd < 0 || fstat(key_fd, &status) < 0) {
        if (key_fd >= 0) {
            (void)close(key_fd);
        }
        return -1;
    }
    if (status.st_uid != 0U || !S_ISREG(status.st_mode) ||
        (status.st_mode & 07777U) != 0400U) {
        (void)close(key_fd);
        return -1;
    }
    return key_fd;
}

static int close_non_target_descriptors(int target_fd)
{
#if defined(SYS_close_range)
    int close_range_available = 1;

    if (target_fd > 3 && syscall(SYS_close_range, 3U, (unsigned int)(target_fd - 1), 0U) < 0) {
        if (errno != ENOSYS) {
            return -1;
        }
        close_range_available = 0;
    }
    if (close_range_available && syscall(SYS_close_range, (unsigned int)(target_fd + 1), UINT_MAX, 0U) < 0) {
        if (errno != ENOSYS) {
            return -1;
        }
        close_range_available = 0;
    }
    if (close_range_available) {
        return 0;
    }
#endif
    {
        struct rlimit limit;
        rlim_t maximum = 65536U;
        int fd;

        if (getrlimit(RLIMIT_NOFILE, &limit) == 0 && limit.rlim_cur < maximum) {
            maximum = limit.rlim_cur;
        }
        for (fd = 3; (rlim_t)fd < maximum; ++fd) {
            if (fd != target_fd) {
                (void)close(fd);
            }
        }
    }
    return 0;
}

static int move_key_to_target(int key_fd)
{
    if (key_fd != TARGET_FD) {
        if (dup2(key_fd, TARGET_FD) < 0 || close(key_fd) < 0) {
            return -1;
        }
    }
    if (fcntl(TARGET_FD, F_SETFD, 0) < 0) {
        return -1;
    }
    return close_non_target_descriptors(TARGET_FD);
}

static int validate_invocation(int argc, char *const argv[], int *root_fd)
{
    size_t count = child_argc();
    size_t index;

    if (argc != (int)(5U + count) || strcmp(argv[1], ROLE_TOKEN) != 0 ||
        strcmp(argv[2], "--implementation-root") != 0 || strcmp(argv[4], "--") != 0 ||
        safe_absolute_path(argv[3]) < 0) {
        return -1;
    }
    for (index = 0U; index < count; ++index) {
        if (strcmp(argv[5U + index], child_argv[index]) != 0) {
            return -1;
        }
    }
    *root_fd = open_safe_directory(argv[3]);
    return *root_fd < 0 ? -1 : 0;
}

int main(int argc, char **argv)
{
    int root_fd = -1;
    int key_fd = -1;

    if (validate_invocation(argc, argv, &root_fd) < 0) {
        return reject_invocation();
    }
    if (fchdir(root_fd) < 0 || close(root_fd) < 0) {
        if (root_fd >= 0) {
            (void)close(root_fd);
        }
        return operation_failed();
    }
    root_fd = -1;
    key_fd = open_signing_key();
    if (key_fd < 0 || move_key_to_target(key_fd) < 0) {
        if (key_fd >= 0) {
            (void)close(key_fd);
        }
        return operation_failed();
    }
    execve(child_argv[0], (char *const *)child_argv, clean_environment);
    return operation_failed();
}
