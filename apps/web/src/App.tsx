import "./App.css";

export function App() {
  return (
    <main className="shell">
      <p className="eyebrow">OSIPTEL · TRASU</p>
      <h1>Respuestas claras sobre reclamos de telecomunicaciones.</h1>
      <p className="intro">
        Reclamos consulta resoluciones públicas y cita el pasaje que respalda
        cada respuesta.
      </p>
      <button type="button" disabled>
        Próximamente podrás hacer una pregunta
      </button>
      <p className="notice">No es asesoría legal.</p>
    </main>
  );
}
