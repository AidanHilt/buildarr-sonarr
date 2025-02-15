# Copyright (C) 2023 Callum Dickinson
#
# Buildarr is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.
#
# Buildarr is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with Buildarr.
# If not, see <https://www.gnu.org/licenses/>.


"""
Sonarr plugin quality settings configuration object.
"""

from __future__ import annotations

import json
from typing import Dict, Optional, cast

from buildarr.config import ConfigTrashIDNotFoundError
from buildarr.state import state
from buildarr.types import TrashID
from pydantic import Field, ValidationInfo, field_validator
from typing_extensions import Annotated, Self

from ..api import api_get, api_put
from ..secrets import SonarrSecrets
from .types import SonarrConfigBase

QUALITYDEFINITION_MIN_MAX = 1998
QUALITYDEFINITION_PREFERRED_MAX = 1999
QUALITYDEFINITION_MAX = 2000

"""
The upper bound for the maximum quality allowed in a quality definition.
"""


class QualityDefinition(SonarrConfigBase):
    """
    Manually set quality definitions can have the following parameters.
    """

    title: Optional[str] = None
    """
    The name of the quality in the GUI.

    If unset, set to an empty string or `None`, it will always be set to the
    name of the quality itself. (e.g. For the `Bluray-480p` quality, the GUI title
    will also be `Bluray-480p`)
    """

    min: Annotated[float, Field(ge=0, le=QUALITYDEFINITION_MIN_MAX)]
    """
    The minimum Megabytes per Minute (MB/min) a quality can have.
    Must be set at least 1MB/min lower than `max`.

    The minimum value is `0`, and the maximum value is `399`.
    """

    preferred: Annotated[float, Field(..., ge=0, le=QUALITYDEFINITION_PREFERRED_MAX)]
    """
    The maximum allowed bitrate for a quality level, in megabytes per minute (MB/min).

    Must be set at least 1MB/min higher than `min`, and 1MB/min lower than `max`.
    If set to `null` or `399`, prefer the highest possible bitrate.
    """

    # Note: No 'pref' field like in Radarr until V4

    max: Optional[Annotated[float, Field(ge=1, le=QUALITYDEFINITION_MAX)]]
    """
    The maximum Megabytes per Minute (MB/min) a quality can have.
    Must be set at least 1MB/min higher than `min`.

    If set to `None` or `400`, the maximum bit rate will be unlimited.

    If not set to `None`, the minimum value is `1`, and the maximum value is `400`.
    """

    preferred: Annotated[float, Field(ge=0)]

    @field_validator("max")
    @classmethod
    def validate_max(cls, value: Optional[float], info: ValidationInfo) -> Optional[float]:
        quality_min: float = info.data["min"]
        if value is not None:
            quality_max = min(value, QUALITYDEFINITION_MAX)
            if quality_max >= QUALITYDEFINITION_MAX:
                value = None
        else:
            quality_max = QUALITYDEFINITION_MAX
        if (quality_max - quality_min) < 1:
            raise ValueError(
                f"'max' ({quality_max}) not greater than 'min' ({quality_min}) by at least 1",
            )
        return value


class SonarrQualitySettingsConfig(SonarrConfigBase):
    """
    Quality definitions are used to set the permitted bit rates for each quality level.

    These can either be set manually within Buildarr, or pre-made profiles can be
    imported from TRaSH-Guides.

    ```yaml
    sonarr:
      settings:
        quality:
          trash_id: "bef99584217af744e404ed44a33af589" # series
          definitions:
            Bluray-480p: # "Quality" column name (not "Title")
              min: 2
              max: 100
            # Add additional override quality definitions here
    ```

    Quality definition profiles retrieved from TRaSH-Guides are automatically
    kept up to date by Buildarr, with the latest values being pushed to Sonarr
    on an update run.

    For more information, refer to the guides from
    [WikiArr](https://wiki.servarr.com/sonarr/settings#quality-1)
    and [TRaSH-Guides](https://trash-guides.info/Sonarr/Sonarr-Quality-Settings-File-Size/).
    """

    # When defined, all explicitly defined quality definitions override the Trash version.
    trash_id: Optional[TrashID] = None
    """
    Trash ID of the TRaSH-Guides quality definition profile to load default values from.

    If there is an update in the profile, the quality definitions will be updated accordingly.
    """

    definitions: Dict[str, QualityDefinition] = {}
    """
    Explicitly set quality definitions here.

    The key of the definition is the "Quality" column of the Quality Definitions page
    in Sonarr, **not** "Title".

    If `trash_id` is set, any values set here will override the default values provided
    from the TRaSH-Guides quality definition profile.

    If `trash_id` is not set, only explicitly defined quality definitions are managed,
    and quality definitions not set within Buildarr are left unmodified.
    """

    def uses_trash_metadata(self) -> bool:
        return bool(self.trash_id)

    def _render(self) -> None:
        if not self.trash_id:
            return
        for quality_file in (
            state.trash_metadata_dir / "docs" / "json" / "sonarr" / "quality-size"
        ).iterdir():
            with quality_file.open() as f:
                quality_json = json.load(f)
                if cast(str, quality_json["trash_id"]).lower() == self.trash_id:
                    for definition_json in quality_json["qualities"]:
                        definition_name = definition_json["quality"]
                        if definition_name not in self.definitions:
                            self.definitions[definition_name] = QualityDefinition(
                                title=None,
                                min=definition_json["min"],
                                preferred=definition_json["preferred"],
                                max=definition_json["max"],
                            )
                    return
        raise ConfigTrashIDNotFoundError(
            f"Unable to find Sonarr quality definition file with trash ID '{self.trash_id}'",
        )

    @classmethod
    def from_remote(cls, secrets: SonarrSecrets) -> Self:
        return cls(
            definitions={
                definition_json["quality"]["name"]: QualityDefinition(
                    title=(
                        definition_json["title"]
                        if definition_json["title"] != definition_json["quality"]["name"]
                        else None
                    ),
                    min=definition_json["minSize"],
                    preferred=definition_json["preferredSize"],
                    max=definition_json.get("maxSize", None),
                )
                for definition_json in api_get(secrets, "/api/v3/qualitydefinition")
            },
        )

    def update_remote(
        self,
        tree: str,
        secrets: SonarrSecrets,
        remote: Self,
        check_unmanaged: bool = False,
    ) -> bool:
        changed = False
        remote_definitions_json = {
            definition_json["id"]: definition_json
            for definition_json in api_get(secrets, "/api/v3/qualitydefinition")
        }
        definition_ids: Dict[str, int] = {
            definition_json["quality"]["name"]: definition_id
            for definition_id, definition_json in remote_definitions_json.items()
        }
        for definition_name, local_definition in self.definitions.items():
            updated, remote_attrs = local_definition.get_update_remote_attrs(
                tree=f"{tree}[{definition_name!r}]",
                remote=remote.definitions[definition_name],
                remote_map=[
                    ("title", "title", {"encoder": lambda v: v or definition_name}),
                    ("min", "minSize", {}),
                    ("preferred", "preferredSize", {}),
                    ("max", "maxSize", {}),
                ],
            )
            if updated:
                definition_id = definition_ids[definition_name]
                api_put(
                    secrets,
                    f"/api/v3/qualitydefinition/{definition_id}",
                    {**remote_definitions_json[definition_id], **remote_attrs},
                )
                changed = True
        return changed
