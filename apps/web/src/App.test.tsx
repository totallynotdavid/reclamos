import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("placeholder page", () => {
  it("explains the supported source and legal boundary in Spanish", () => {
    render(<App />);

    expect(screen.getByRole("heading")).toHaveTextContent("reclamos");
    expect(screen.getByText(/resoluciones públicas/i)).toBeInTheDocument();
    expect(screen.getByText(/no es asesoría legal/i)).toBeInTheDocument();
  });
});
