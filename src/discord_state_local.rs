#![allow(dead_code)]
//! Local capability adapter for notification state.

use std::path::Path;

use super::discord_state::NotificationStateStore;
use super::discord_state_lane::StateLaneHandle;
use super::discord_state_schema::NotificationStateError;

pub(crate) struct LocalStateStore {
    lane: StateLaneHandle,
}

impl LocalStateStore {
    pub(crate) fn open(state_file: &Path) -> Result<Self, NotificationStateError> {
        let parent = state_file
            .parent()
            .ok_or(NotificationStateError::InvalidInput)?;
        std::fs::create_dir_all(parent).map_err(|_| NotificationStateError::Io)?;
        let notification_dir = parent.join("discord-notifications");
        std::fs::create_dir_all(&notification_dir).map_err(|_| NotificationStateError::Io)?;
        let path = notification_dir.join("state.json");
        let lock = notification_dir.join("state.json.lock");
        Ok(Self {
            lane: StateLaneHandle::open(path, lock)?,
        })
    }
}

impl NotificationStateStore for LocalStateStore {
    fn lane(&self) -> &StateLaneHandle {
        &self.lane
    }
}
