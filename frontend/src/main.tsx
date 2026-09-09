import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@/lib/gsap"; // register the GSAP React plugin (side effect)
import App from "@/App";
import "@/index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
