import type enCommon from "@i18n/en/common.json";
import type enConsole from "@i18n/en/console.json";

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "console";
    resources: {
      common: typeof enCommon;
      console: typeof enConsole;
    };
    returnNull: false;
  }
}
