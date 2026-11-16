# cableCORE app notes

Facts verified in chats (newest first). Edit or delete freely.

- DesignCenter formulas use JavaScript-style conditional expressions (`condition ? valueIfTrue : valueIfFalse`); a plant conductor CSA reference is written as `plant.conductorCSA`, so the CableBuilder example converts to `(plant.conductorCSA <= 50) ? 1.0 : 1.2`. (evidence: The running formula language parses and evaluates ConditionalExpression nodes, and plant attributes are referenced through the `plant.<attribute>` namespace; the formula editor's built-in function list does not include IF.; 2026-11-16) <!-- sources: cableCORE_BE/src/formula-execution/ast/interpreters/ast-basic-evaluators.service.ts@0b48758201f5, cableCORE_BE/src/formula-execution/ast/workers/formula-eval-ast.ts@2e47793a048a, cableCORE_FE/src/lib/customLanguage/constants.ts@bd2d3d49b5f9 -->
