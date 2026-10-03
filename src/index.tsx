import { staticClasses } from "@decky/ui";
import { addEventListener, definePlugin, removeEventListener } from "@decky/api";
import { FaGlasses } from "react-icons/fa";

import { Content } from "./Content";
import { STATE_EVENT } from "./api";
import { deviceStore } from "./deviceStore";
import type { DeviceState } from "./types";

/**
 * Plugin entry point.
 *
 * This callback is a plain factory (DefinePluginFn = () => Plugin), not a
 * component, so no React hook may be called here -- there is no active
 * dispatcher and a hook call throws, blanking the whole panel. Event
 * subscription therefore lives here too, with onDismount as the cleanup hook,
 * which is what the official plugin template does.
 */
export default definePlugin(() => {
  const onState = (state: DeviceState) => deviceStore.setState(state);
  const listener = addEventListener<[DeviceState]>(STATE_EVENT, onState);

  return {
    name: "RayNeo Control",
    titleView: <div className={staticClasses.Title}>RayNeo 眼镜控制</div>,
    content: <Content />,
    icon: <FaGlasses />,
    onDismount() {
      removeEventListener(STATE_EVENT, listener);
      deviceStore.reset();
    },
  };
});