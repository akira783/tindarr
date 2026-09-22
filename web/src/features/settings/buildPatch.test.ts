import { describe, expect, it } from "vitest";

import * as fixtures from "../../../test/fixtures";
import { buildPatch } from "./SettingsPage";

const current = fixtures.settings();

const unchanged = {
  name: current.name,
  publicUrl: current.public_url ?? "",
  passwordSignIn: current.password_sign_in,
  language: current.language,
  streamingRegion: current.streaming_region ?? "",
  dailyGenerationLimit: String(current.daily_generation_limit),
  warmUpEnabled: current.warm_up_enabled,
  excludeAdult: current.content_filters.exclude_adult,
  minYear: "",
  excludedGenres: "",
  excludedLanguages: "",
};

describe("buildPatch", () => {
  it("sends nothing when nothing changed", () => {
    expect(buildPatch(current, unchanged)).toEqual({});
  });

  it("clears the public address with null rather than an empty string", () => {
    expect(buildPatch(current, { ...unchanged, publicUrl: "" })).toEqual({ public_url: null });
  });

  it("normalises the streaming region and clears it with null", () => {
    expect(buildPatch(current, { ...unchanged, streamingRegion: "be" })).toEqual({
      streaming_region: "BE",
    });
    expect(buildPatch(current, { ...unchanged, streamingRegion: "" })).toEqual({
      streaming_region: null,
    });
  });

  it("sends the fields kept for later steps as the contract types them", () => {
    expect(
      buildPatch(current, {
        ...unchanged,
        passwordSignIn: "lan_only",
        language: "en-GB",
        dailyGenerationLimit: "0",
        warmUpEnabled: false,
      }),
    ).toEqual({
      password_sign_in: "lan_only",
      language: "en-GB",
      daily_generation_limit: 0,
      warm_up_enabled: false,
    });
  });

  it("rebuilds the whole content filter when one of its parts changes", () => {
    expect(
      buildPatch(current, {
        ...unchanged,
        excludeAdult: false,
        minYear: "1980",
        excludedGenres: "horror, reality ,",
        excludedLanguages: "ja",
      }),
    ).toEqual({
      content_filters: {
        exclude_adult: false,
        min_year: 1980,
        excluded_genres: ["horror", "reality"],
        excluded_original_languages: ["ja"],
      },
    });
  });
});
