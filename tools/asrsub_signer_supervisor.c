#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
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
static const char *const required_directories[] = {
    "tools",
    "release",
    "release/runtime",
    "release/systemd",
    "release/systemd/docker.service.d",
    NULL,
};
static const char *const required_files[] = {
    "tools/asrsub-env",
    "tools/package_bundle.py",
    "release/runtime/asrsub",
    "release/runtime/asrsub-state",
    "release/runtime/asrsub-record-rollout",
    "release/runtime/asrsub-generate-media-runtime-manifest",
    "release/runtime/asrsub-recover",
    "release/runtime/asrsub-runtime",
    "release/runtime/asrsub-health-probe",
    "release/runtime/asrsub-provision-statefs",
    "release/runtime/media-runtime-dependencies.json",
    "release/runtime/production_entrypoint.py",
    "release/runtime/production_adapter_common.py",
    "release/runtime/deploy_docker.py",
    "release/runtime/compose.yaml",
    "release/systemd/asrsub-recovery.service",
    "release/systemd/asrsub-runtime.service",
    "release/systemd/docker.service.d/asrsub-recovery.conf",
    NULL,
};
static const char *const required_trees[] = {
    "release/runtime",
    "release/systemd",
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
static const char *const required_directories[] = {
    "tools",
    "release",
    NULL,
};
static const char *const required_files[] = {
    "tools/asrsub-env",
    "tools/create_approval.py",
    "release/approval-canonical.json",
    NULL,
};
static const char *const required_trees[] = {
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
    if (status.st_uid != 0U) {
        return -1;
    }
    mode = status.st_mode;
    if ((mode & (S_ISUID | S_ISGID)) != 0U ||
        ((mode & (S_IWGRP | S_IWOTH)) != 0U && (mode & S_ISVTX) == 0U)) {
        return -1;
    }
    return 0;
}

static int safe_regular_file_fd(int fd)
{
    struct stat status;

    if (fstat(fd, &status) < 0 || !S_ISREG(status.st_mode) || status.st_uid != 0U ||
        (status.st_mode & (S_ISUID | S_ISGID | S_IWGRP | S_IWOTH)) != 0U) {
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

static int open_relative_directory(int root_fd, const char *path)
{
    int current;
    const char *cursor = path;

    if (path == NULL || path[0] == '\0' || path[0] == '/') {
        return -1;
    }
    current = fcntl(root_fd, F_DUPFD_CLOEXEC, 3);
    if (current < 0) {
        return -1;
    }
    while (*cursor != '\0') {
        const char *start = cursor;
        size_t length;
        char component[NAME_MAX + 1U];
        int next;

        while (*cursor != '\0' && *cursor != '/') {
            ++cursor;
        }
        length = (size_t)(cursor - start);
        if (length == 0U || length > NAME_MAX ||
            (length == 1U && start[0] == '.') ||
            (length == 2U && start[0] == '.' && start[1] == '.')) {
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

static int open_relative_file(int root_fd, const char *path)
{
    const char *separator;
    const char *name;
    int directory_fd;
    int file_fd;
    char parent[PATH_MAX];
    size_t parent_length;

    if (path == NULL || path[0] == '\0' || path[0] == '/') {
        return -1;
    }
    separator = strrchr(path, '/');
    if (separator == NULL) {
        directory_fd = fcntl(root_fd, F_DUPFD_CLOEXEC, 3);
        name = path;
    } else {
        parent_length = (size_t)(separator - path);
        if (parent_length == 0U || parent_length >= sizeof(parent)) {
            return -1;
        }
        (void)memcpy(parent, path, parent_length);
        parent[parent_length] = '\0';
        directory_fd = open_relative_directory(root_fd, parent);
        name = separator + 1;
    }
    if (directory_fd < 0 || name[0] == '\0' || strchr(name, '/') != NULL ||
        (strcmp(name, ".") == 0) || (strcmp(name, "..") == 0)) {
        if (directory_fd >= 0) {
            (void)close(directory_fd);
        }
        return -1;
    }
    file_fd = openat(directory_fd, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    (void)close(directory_fd);
    if (file_fd < 0 || safe_regular_file_fd(file_fd) < 0) {
        if (file_fd >= 0) {
            (void)close(file_fd);
        }
        return -1;
    }
    return file_fd;
}

static int validate_tree_fd(int directory_fd)
{
    int scan_fd;
    DIR *stream;
    struct dirent *entry;
    int result = 0;

    scan_fd = fcntl(directory_fd, F_DUPFD_CLOEXEC, 3);
    if (scan_fd < 0) {
        return -1;
    }
    stream = fdopendir(scan_fd);
    if (stream == NULL) {
        (void)close(scan_fd);
        return -1;
    }
    for (;;) {
        int child_fd;
        struct stat status;

        errno = 0;
        entry = readdir(stream);
        if (entry == NULL) {
            if (errno != 0) {
                result = -1;
            }
            break;
        }

        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
            continue;
        }
        child_fd = openat(directory_fd, entry->d_name, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
        if (child_fd < 0 || fstat(child_fd, &status) < 0) {
            if (child_fd >= 0) {
                (void)close(child_fd);
            }
            result = -1;
            break;
        }
        if (S_ISDIR(status.st_mode)) {
            if (safe_final_directory_fd(child_fd) < 0 || validate_tree_fd(child_fd) < 0) {
                result = -1;
            }
        } else if (safe_regular_file_fd(child_fd) < 0) {
            result = -1;
        }
        (void)close(child_fd);
        if (result < 0) {
            break;
        }
    }
    if (closedir(stream) < 0) {
        result = -1;
    }
    return result;
}

static int validate_root_contents(int root_fd)
{
    size_t index;

    for (index = 0U; required_directories[index] != NULL; ++index) {
        int directory_fd = open_relative_directory(root_fd, required_directories[index]);

        if (directory_fd < 0) {
            return -1;
        }
        (void)close(directory_fd);
    }
    for (index = 0U; required_files[index] != NULL; ++index) {
        int file_fd = open_relative_file(root_fd, required_files[index]);

        if (file_fd < 0) {
            return -1;
        }
        (void)close(file_fd);
    }
    for (index = 0U; required_trees[index] != NULL; ++index) {
        int directory_fd = open_relative_directory(root_fd, required_trees[index]);

        if (directory_fd < 0 || validate_tree_fd(directory_fd) < 0) {
            if (directory_fd >= 0) {
                (void)close(directory_fd);
            }
            return -1;
        }
        (void)close(directory_fd);
    }
    return 0;
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
        DIR *stream = opendir("/proc/self/fd");
        struct dirent *entry;
        int directory_fd;
        int result = 0;

        if (stream == NULL) {
            return -1;
        }
        directory_fd = dirfd(stream);
        for (;;) {
            char *end = NULL;
            long value;

            errno = 0;
            entry = readdir(stream);
            if (entry == NULL) {
                if (errno != 0) {
                    result = -1;
                }
                break;
            }

            value = strtol(entry->d_name, &end, 10);
            if (end == entry->d_name || *end != '\0' || value < 3L || value > INT_MAX ||
                value == (long)target_fd || value == (long)directory_fd) {
                continue;
            }
            if (close((int)value) < 0 && errno != EBADF) {
                result = -1;
            }
        }
        if (closedir(stream) < 0) {
            result = -1;
        }
        return result;
    }
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
    if (*root_fd < 0 || validate_root_contents(*root_fd) < 0) {
        if (*root_fd >= 0) {
            (void)close(*root_fd);
            *root_fd = -1;
        }
        return -1;
    }
    return 0;
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
