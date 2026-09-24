import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  listConnectors,
  listLlmModels,
  type Connector,
  type ConnectorInput,
  type ConnectorKind,
  type LlmProviderKind,
  type LlmSettingsInput,
} from "../../api/operations";
import {
  CheckboxField,
  ErrorAlert,
  Loading,
  SelectField,
  TextField,
} from "../../components/ui";
import { Button } from "../../components/ui";
import { ConnectorCard, ConnectorHealthAlert, SecretHint } from "./ConnectorCard";

const CONNECTORS_KEY = ["admin", "connectors"] as const;

/** Providers that need an address of their own, and the one that needs no key. */
const NEEDS_BASE_URL: ReadonlySet<string> = new Set(["openai_compatible", "ollama"]);
const NEEDS_API_KEY: ReadonlySet<string> = new Set([
  "openai",
  "anthropic",
  "gemini",
  "mistral",
]);
const PROVIDERS: LlmProviderKind[] = [
  "openai",
  "anthropic",
  "gemini",
  "mistral",
  "openai_compatible",
  "ollama",
];

export function ConnectorsPage(): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const connectors = useQuery({
    queryKey: CONNECTORS_KEY,
    queryFn: listConnectors,
    retry: false,
  });

  if (connectors.isPending) return <Loading />;
  if (connectors.error !== null) return <ErrorAlert error={connectors.error} />;

  const byKind = new Map<ConnectorKind, Connector>(
    connectors.data.map((row) => [row.kind, row]),
  );
  const find = (kind: ConnectorKind): Connector =>
    byKind.get(kind) ?? {
      kind,
      configured: false,
      secret: { set: false },
      status: { health: "not_configured" },
    };

  return (
    <main id="main" className="page">
      <h1>{t("connectors.title")}</h1>
      <p>{t("connectors.intro")}</p>
      <MediaServerCard connector={find("media_server")} />
      <ApiKeyCard connector={find("tmdb")} kind="tmdb" />
      <ApiKeyCard connector={find("omdb")} kind="omdb" />
      <RequestsCard connector={find("requests")} />
      <LlmCard connector={find("llm")} />
    </main>
  );
}

function useRefresh(): () => void {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: CONNECTORS_KEY });
  };
}

/**
 * The media server is shown here but changed in the setup wizard: repointing it needs
 * a media server administrator with a fresh re-authentication, and it decides who can
 * sign in at all (docs/auth.md, section 6). Testing it costs nothing, so that stays.
 */
function MediaServerCard({ connector }: { connector: Connector }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  return (
    <section className="card">
      <h2>{t("connectors.mediaServer.title")}</h2>
      <p className="hint">{t("connectors.mediaServer.help")}</p>
      <ConnectorHealthAlert status={connector.status} />
      <dl className="summary">
        <dt>{t("connectors.mediaServer.provider")}</dt>
        <dd>{connector.provider ?? t("common:state.none")}</dd>
        <dt>{t("connectors.url")}</dt>
        <dd>{connector.url ?? t("common:state.none")}</dd>
      </dl>
      <SecretHint connector={connector} />
    </section>
  );
}

function ApiKeyCard({
  connector,
  kind,
}: {
  connector: Connector;
  kind: "tmdb" | "omdb";
}): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const [apiKey, setApiKey] = useState("");
  const refresh = useRefresh();
  const locked = new Set(connector.locked_fields ?? []);

  const body = (): ConnectorInput => ({
    connector: kind,
    ...(apiKey === "" || locked.has("api_key") ? {} : { api_key: apiKey }),
  });

  return (
    <ConnectorCard
      connector={connector}
      title={t(`connectors.${kind}.title`)}
      help={t(`connectors.${kind}.help`)}
      body={body}
      valid={connector.secret?.set === true || apiKey !== ""}
      onChanged={() => {
        setApiKey("");
        refresh();
      }}
    >
      <TextField
        label={t("connectors.apiKey")}
        type="password"
        autoComplete="off"
        value={apiKey}
        disabled={locked.has("api_key")}
        locked={locked.has("api_key")}
        lockedNote={t("connectors.locked")}
        onChange={(event) => {
          setApiKey(event.target.value);
        }}
      />
    </ConnectorCard>
  );
}

function RequestsCard({ connector }: { connector: Connector }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const refresh = useRefresh();
  const locked = new Set(connector.locked_fields ?? []);
  const [url, setUrl] = useState(connector.url ?? "");
  const [apiKey, setApiKey] = useState("");
  const [verifyTls, setVerifyTls] = useState(connector.verify_tls ?? true);
  const [seasons, setSeasons] = useState<"all" | "first">(
    connector.tv_seasons === "first" ? "first" : "all",
  );

  const body = (): ConnectorInput => ({
    connector: "requests",
    url,
    ...(apiKey === "" || locked.has("api_key") ? {} : { api_key: apiKey }),
    ...(locked.has("verify_tls") ? {} : { verify_tls: verifyTls }),
    ...(locked.has("tv_seasons") ? {} : { tv_seasons: seasons }),
  });

  return (
    <ConnectorCard
      connector={connector}
      title={t("connectors.requests.title")}
      help={t("connectors.requests.help")}
      body={body}
      valid={url !== ""}
      onChanged={() => {
        setApiKey("");
        refresh();
      }}
    >
      <TextField
        label={t("connectors.url")}
        hint={t("connectors.requests.urlHint")}
        type="url"
        required
        value={url}
        disabled={locked.has("url")}
        locked={locked.has("url")}
        lockedNote={t("connectors.locked")}
        onChange={(event) => {
          setUrl(event.target.value);
        }}
      />
      <TextField
        label={t("connectors.apiKey")}
        type="password"
        autoComplete="off"
        value={apiKey}
        disabled={locked.has("api_key")}
        locked={locked.has("api_key")}
        lockedNote={t("connectors.locked")}
        onChange={(event) => {
          setApiKey(event.target.value);
        }}
      />
      <SelectField
        label={t("connectors.requests.seasons")}
        hint={t("connectors.requests.seasonsHint")}
        value={seasons}
        disabled={locked.has("tv_seasons")}
        onChange={(event) => {
          setSeasons(event.target.value === "first" ? "first" : "all");
        }}
        options={[
          { value: "all", label: t("connectors.requests.seasonsAll") },
          { value: "first", label: t("connectors.requests.seasonsFirst") },
        ]}
      />
      <CheckboxField
        label={t("connectors.verifyTls")}
        hint={t("connectors.verifyTlsHint")}
        checked={verifyTls}
        disabled={locked.has("verify_tls")}
        onChange={(event) => {
          setVerifyTls(event.target.checked);
        }}
      />
    </ConnectorCard>
  );
}

function LlmCard({ connector }: { connector: Connector }): ReactNode {
  const { t } = useTranslation(["console", "common"]);
  const refresh = useRefresh();
  const locked = new Set(connector.locked_fields ?? []);
  const [provider, setProvider] = useState<LlmProviderKind>(
    (connector.provider as LlmProviderKind | null) ?? "openai",
  );
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(connector.url ?? "");
  const [model, setModel] = useState(connector.model ?? "");
  const [effort, setEffort] = useState<"" | "low" | "medium" | "high">("");

  const settings = (): LlmSettingsInput => ({
    connector: "llm",
    provider,
    ...(apiKey === "" || locked.has("api_key") ? {} : { api_key: apiKey }),
    ...(NEEDS_BASE_URL.has(provider) && baseUrl !== "" ? { base_url: baseUrl } : {}),
    ...(model === "" ? {} : { model }),
    ...(effort === "" ? {} : { reasoning_effort: effort }),
  });

  const models = useMutation({
    mutationFn: () => listLlmModels(settings()),
  });

  const needsKey = NEEDS_API_KEY.has(provider);
  const valid =
    (!needsKey || connector.secret?.set === true || apiKey !== "") &&
    (!NEEDS_BASE_URL.has(provider) || baseUrl !== "");

  return (
    <ConnectorCard
      connector={connector}
      title={t("connectors.llm.title")}
      help={t("connectors.llm.help")}
      body={settings}
      valid={valid}
      onChanged={() => {
        setApiKey("");
        refresh();
      }}
    >
      <SelectField
        label={t("connectors.llm.provider")}
        value={provider}
        disabled={locked.has("provider")}
        locked={locked.has("provider")}
        lockedNote={t("connectors.locked")}
        onChange={(event) => {
          setProvider(event.target.value as LlmProviderKind);
          models.reset();
        }}
        options={PROVIDERS.map((kind) => ({
          value: kind,
          label: t(`connectors.llm.providers.${kind}`),
        }))}
      />
      {/* Every provider takes a key: a generic OpenAI-compatible endpoint or an Ollama
          behind a proxy often needs one, and hiding the field left those unconfigurable.
          Only the named providers make it mandatory (`needsKey`). */}
      <TextField
        label={needsKey ? t("connectors.apiKey") : t("connectors.apiKeyOptional")}
        type="password"
        autoComplete="off"
        value={apiKey}
        disabled={locked.has("api_key")}
        locked={locked.has("api_key")}
        lockedNote={t("connectors.locked")}
        onChange={(event) => {
          setApiKey(event.target.value);
        }}
      />
      {NEEDS_BASE_URL.has(provider) && (
        <TextField
          label={t("connectors.llm.baseUrl")}
          hint={t("connectors.llm.baseUrlHint")}
          type="url"
          required
          value={baseUrl}
          disabled={locked.has("base_url")}
          locked={locked.has("base_url")}
          lockedNote={t("connectors.locked")}
          onChange={(event) => {
            setBaseUrl(event.target.value);
          }}
        />
      )}
      <TextField
        label={t("connectors.llm.model")}
        hint={t("connectors.llm.modelHint")}
        value={model}
        disabled={locked.has("model")}
        locked={locked.has("model")}
        lockedNote={t("connectors.locked")}
        onChange={(event) => {
          setModel(event.target.value);
        }}
      />
      <div className="row">
        <Button
          onClick={() => {
            models.mutate();
          }}
          disabled={models.isPending || !valid}
        >
          {t("connectors.llm.listModels")}
        </Button>
      </div>
      <ErrorAlert error={models.error} />
      {models.data !== undefined && (
        <SelectField
          label={t("connectors.llm.pickModel")}
          hint={t("connectors.llm.pickModelHint")}
          value={models.data.includes(model) ? model : ""}
          onChange={(event) => {
            setModel(event.target.value);
          }}
          options={[
            { value: "", label: t("connectors.llm.typeOne") },
            ...models.data.map((id) => ({ value: id, label: id })),
          ]}
        />
      )}
      <SelectField
        label={t("connectors.llm.reasoningEffort")}
        hint={t("connectors.llm.reasoningEffortHint")}
        value={effort}
        disabled={locked.has("reasoning_effort")}
        onChange={(event) => {
          setEffort(event.target.value as "" | "low" | "medium" | "high");
        }}
        options={[
          { value: "", label: t("connectors.llm.effortDefault") },
          { value: "low", label: t("connectors.llm.effortLow") },
          { value: "medium", label: t("connectors.llm.effortMedium") },
          { value: "high", label: t("connectors.llm.effortHigh") },
        ]}
      />
    </ConnectorCard>
  );
}
