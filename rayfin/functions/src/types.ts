/**
 * Function schema types for RayfinClient.
 *
 * AUTO-GENERATED — do not edit manually.
 * Re-generated automatically when function source files change.
 *
 * If this file is not updating automatically, run:
 *   rayfin dev functions apply
 *
 * The schema is a closed object type: only the function names listed
 * below are accepted by RayfinClient.functions.<name>.invoke(...).
 * Adding, renaming, or changing the signature of a udf.func() call
 * regenerates this file and surfaces type errors at every consumer.
 *
 * IMPORTANT: This file must NOT import any Node.js packages — it is
 * resolved by the frontend app's TypeScript compiler.
 */

export type AppFunctionsSchema = {
  getCohort: {
    input: Record<string, never>;
    output: { patients: { patient_id: string; display_name: string; cancer_code: string; cancer_type: string; organ: string; sex: null | string; age_at_diagnosis: null | number; race: null | string; stage: null | string; vital_status: null | string; year_of_diagnosis: null | number; days_to_last_contact: null | number; tmb: null | number; msi_status: null | string; subtype: null | string; drivers: null | string; slide_gigapixels: null | number; radiology_series: number; thumbnailUrl: null | string }[]; provenance: { workspace: string; endpoint: string; database: string; queries: { id: string; silos: ('clinical' | 'genomics' | 'imaging')[]; tables: string[]; rows: number; ms: number }[]; totalMs: number; showcaseSql: string; servedAt: string } };
  };
  getPatient: {
    input: { patientId: string };
    output: { patient: { [k: string]: null | string | number | false | true }; header: { [k: string]: null | string | number | false | true }; diagnoses: { [k: string]: null | string | number | false | true }[]; treatments: { [k: string]: null | string | number | false | true }[]; followUps: { [k: string]: null | string | number | false | true }[]; biomarkers: { [k: string]: null | string | number | false | true }[]; observations: { [k: string]: null | string | number | false | true }[]; encounters: { [k: string]: null | string | number | false | true }[]; conditions: { [k: string]: null | string | number | false | true }[]; medications: { [k: string]: null | string | number | false | true }[]; immunizations: { [k: string]: null | string | number | false | true }[]; allergies: { [k: string]: null | string | number | false | true }[]; procedures: { [k: string]: null | string | number | false | true }[]; careplans: { [k: string]: null | string | number | false | true }[]; report: { text: string; pdfPath: null | string; pdfBytes: null | number; gdcFileId: null | string; source: null | string }; summary: null | { [k: string]: null | string | number | false | true }; profile: null | { [k: string]: null | string | number | false | true }; drivers: { [k: string]: null | string | number | false | true }[]; mutationMap: { chromosomes: string[]; positions: number[]; types: number[]; typeNames: string[] }; mutationCounts: { [k: string]: null | string | number | false | true }[]; segments: { [k: string]: null | string | number | false | true }[]; copyNumber: { [k: string]: null | string | number | false | true }[]; expression: { [k: string]: null | string | number | false | true }[]; immuneCells: { [k: string]: null | string | number | false | true }[]; reference: { [k: string]: null | string | number | false | true }[]; slide: null | { [k: string]: null | string | number | false | true }; slides: { [k: string]: null | string | number | false | true }[]; radiologyStudies: { [k: string]: null | string | number | false | true }[]; radiologySeries: { [k: string]: null | string | number | false | true }[]; access: { slide: { baseUrl: string; query: string; expiresOn: string; scope: string; silo: 'clinical' | 'genomics' | 'imaging'; error?: undefined | string }; radiology: { baseUrl: string; query: string; expiresOn: string; scope: string; silo: 'clinical' | 'genomics' | 'imaging'; error?: undefined | string }; report: { baseUrl: string; query: string; expiresOn: string; scope: string; silo: 'clinical' | 'genomics' | 'imaging'; error?: undefined | string } }; provenance: { workspace: string; endpoint: string; database: string; queries: { id: string; silos: ('clinical' | 'genomics' | 'imaging')[]; tables: string[]; rows: number; ms: number }[]; totalMs: number; showcaseSql: string; servedAt: string } };
  };
  getDataMap: {
    input: Record<string, never>;
    output: { workspace: { id: string; name: string; portalUrl: string }; patient360: { id: string; name: string; portalUrl: string; sqlEndpoint: string; shortcuts: { path: string; name: string; targetItem: string; targetPath: string; silo: null | 'clinical' | 'genomics' | 'imaging' }[]; error?: undefined | string }; silos: { silo: 'clinical' | 'genomics' | 'imaging'; id: string; name: string; role: string; portalUrl: string; items: { [k: string]: null | string | number | false | true }[] }[]; provenance: { workspace: string; endpoint: string; database: string; queries: { id: string; silos: ('clinical' | 'genomics' | 'imaging')[]; tables: string[]; rows: number; ms: number }[]; totalMs: number; showcaseSql: string; servedAt: string } };
  };
};
