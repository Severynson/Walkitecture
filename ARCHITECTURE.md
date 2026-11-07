Backend endpoints:

- login
- (uplod DWG -> runs the process of Babylon.js code generation, writing it to the database for a particular DWG. We're going to use)
-

Modules:

## DWG is being read like in helper/read_dwg.py and is being transformed to descriptive JSON (lines, circles, isert, etc.) to define what elements are walls, what elements are windows, doors, and which are other complex 3-D objects such as furniture beside their shapes;
- For the start create a test script which could turn the @examples/call-center-offices.dwg into a descriptive JSON (extract shapes so I could see what information the following part of the APP will get).

## JSON into the Babylon.js code convertor

Simple shapes such as lines, circles, arc, and lwpolyline which appear to be windows, dorframes/doors have to be generated automatically with the algorithmic part.

- Walls turn into walls fairly easy - just add a constant height for walls (e.g. 10 inches). All the walls are the same height.
- Windows and dorframes/doors - just make all the doorframes 7 feet in height, starting at height 0. Over it, and for windows also under it - add a wall of the same thickness as on the right and on the left of it, and it has to end on the top and on the bottom on the same height as walls on the right and on the left. Windows must start on the height 2.5 feets, and end on the height of 6.5 feets.
- At this point we want to test if it is possible to put furniture only with algorithmic code without using LLM-based AI agent. The most likely it must be possible to identify INSWERT equivalents of DWG and find what object type they are (e.g. table, chair, sofas, desks, closet, etc) - Then we can just find the most similar object from our GLB-furniture library in @frontend/furniture. If there is something similar, just use this item. And we could try to place them also without LLM just using a rule that furniture is always dirrected away from the wall by its front side to turn it by the right side. But if this approach doesn't work - it is worth to implement furniture placement with LLM API (so AI would decide in which room where it is logical to place what furniture).